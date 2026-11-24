import os
import re
import time
from datetime import datetime, timezone, timedelta
from google import genai
from google.genai import errors, types
from github import Github, Auth

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO_NAME = os.getenv("GITHUB_REPOSITORY")
ISSUE_NUMBER = int(os.getenv("ISSUE_NUMBER", "0"))

auth = Auth.Token(GITHUB_TOKEN)
gh = Github(auth=auth)
repo = gh.get_repo(REPO_NAME)

UPDATE_SYSTEM_PROMPT = """
你是一位專精於「工程與理工科目（特別是電力電子、控制系統、電路學）」的助教與知識工程師。
你的任務是根據使用者在 GitHub Issue 中的補充回答，無縫整合並更新原有的課堂 Markdown 筆記。

【核心更新鐵則（務必嚴格遵守）】
1. **完整保留既有深度與結構**：絕對不可將原有的詳細公式推導濃縮成簡短摘要！必須保留所有既有的章節結構、Step-by-Step 數學推導步驟、物理意義解說與比較表格，並將使用者的最新回答自然融入對應章節中。
2. **完整保留所有圖片語法**：原筆記中所有的 `> 🖼️ **...**` 與 `![...](assets/...)` 圖片連結必須原封不動保留在正確的段落位置，絕對不可刪除或修改圖片路徑。
3. **一次只問一個關鍵問題**：如果原板書或最新討論中仍有尚未釐清的模糊處，請在 `===NEXT_QUESTION===` 提出一個最關鍵的具體問題；若全部已釐清，請填 `NONE`。

請嚴格依照以下 3 個區塊標籤格式輸出（請務必保留 ===標籤=== 字樣，不要輸出 JSON）：

===SUMMARY===
更新後的 4~6 個重點核心精華摘要（Markdown 格式）

===NOTES===
整合使用者補充回答後的完整結構化深度筆記（Markdown 格式，不含最外層的 `## 📖 結構化重點筆記` 標題，直接從 `## 1. ...` 開始）

===NEXT_QUESTION===
下一個待釐清問題（若無請寫 NONE）

【GitHub LaTeX 公式排版嚴格規範（極重要）】：
1. 行內公式請使用單個錢字號 `$公式$`，獨立區塊公式請使用雙錢字號 `$$公式$$` 並前後空行。
2. 絕對禁止將 `$公式$` 緊貼半形括號或中文字，外側請務必各留一個半形空白（例如：`（ $0 \\sim D_1 T$ ）`）。
3. 區塊公式 `$$` 內部絕對不能有空行，且開頭與結尾的 `$$` 必須獨佔一行。
"""

def parse_update_response(text: str) -> dict:
    def extract_section(tag_name, next_tags):
        next_pattern = "|".join([f"==={t}===" for t in next_tags])
        pattern = rf"==={tag_name}===\s*([\s\S]*?)(?=(?:{next_pattern})|$)"
        match = re.search(pattern, text)
        return match.group(1).strip() if match else ""

    summary = extract_section("SUMMARY", ["NOTES", "NEXT_QUESTION"])
    notes = extract_section("NOTES", ["NEXT_QUESTION"])
    next_q = extract_section("NEXT_QUESTION", [])

    if not summary or not notes:
        raise ValueError("Gemini 回覆缺少摘要或正文，未覆蓋原筆記。")

    if next_q.upper() in ("NONE", "NULL", ""):
        next_q = None

    return {
        "summary": summary,
        "notes": notes,
        "next_question": next_q
    }

def fix_github_latex(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r", "")

    def format_block_math(match):
        content = match.group(1).strip()
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        clean_content = "\n".join(lines)
        return f"\n\n$$\n{clean_content}\n$$\n\n"

    text = re.sub(r'\$\$([\s\S]+?)\$\$', format_block_math, text)

    def format_inline_math(match):
        inner = match.group(1).strip()
        return f" ${inner}$ "

    text = re.sub(r'(?<!\$)\$(?!\$)([^\$\n]+?)(?<!\$)\$(?!\$)', format_inline_math, text)
    text = re.sub(r'[ ]{2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def call_gemini_with_retry(contents, config):
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY 未設定，請檢查 Secrets。")

    def generate(model, attempts):
        http_options = types.HttpOptions(
            timeout=180_000,
            retry_options=types.HttpRetryOptions(
                attempts=attempts,
                initial_delay=5,
                max_delay=30,
                exp_base=2,
                jitter=2,
                http_status_codes=[408, 429, 500, 502, 503, 504],
            ),
        )
        with genai.Client(api_key=GEMINI_API_KEY, http_options=http_options) as client:
            return client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )

    fallback_chain = [
        ("gemini-3.8-flash", 5),
        ("gemini-3.5-flash-lite", 3),
    ]

    last_error = None
    for idx, (model_name, attempts) in enumerate(fallback_chain):
        try:
            if idx > 0:
                print(f"🔄 切換備援模型嘗試：{model_name}...")
            return generate(model_name, attempts=attempts)
        except errors.APIError as error:
            last_error = error
            if error.code not in (429, 500, 502, 503, 504):
                raise
            print(f"⚠️ {model_name} 暫時無法使用（HTTP {error.code}）...")

    raise last_error

def process_comment():
    issue = repo.get_issue(number=ISSUE_NUMBER)

    match_note = re.search(r"`(notes/[^`]+\.md)`", issue.body)
    match_pdf = re.search(r"在閱讀 `([^`]+\.pdf)`", issue.body)
    if not match_note:
        print("此 Issue 非筆記補全任務，略過。")
        return

    note_path = match_note.group(1)
    pdf_path = match_pdf.group(1) if match_pdf else ""

    if not os.path.exists(note_path):
        print(f"找不到筆記檔案: {note_path}")
        return

    with open(note_path, "r", encoding="utf-8") as f:
        current_note = f.read()

    lines = current_note.splitlines()
    first_line = lines[0] if lines else f"# 課堂筆記：{note_path}"

    # 保留原本的 metadata 行（科目、上課日期、原始檔案）
    meta_line = ""
    for line in lines[1:5]:
        if line.startswith("> **科目**：") or line.startswith("> **原始檔案**："):
            meta_line = line
            break

    # 萃取並完整保留原本的「原始板書/講義高畫質對照」折疊區塊
    gallery_match = re.search(
        r"(## 🖼️ 原始板書/講義高畫質對照[\s\S]*?</details>)",
        current_note
    )
    gallery_section = f"{gallery_match.group(1)}\n\n---\n\n" if gallery_match else ""

    # 萃取原本的「後續複習提示詞」區塊（若有）
    prompt_match = re.search(
        r"(## 💡 後續複習提示詞 \(Prompt\)[\s\S]*)$",
        current_note
    )
    if prompt_match:
        prompt_section = prompt_match.group(1).strip()
    else:
        prompt_section = (
            "## 💡 後續複習提示詞 (Prompt)\n"
            "```markdown\n"
            "你現在是該領域的教學專家。請根據上述筆記內容，出 3 題觀念驗證題（包含 1 題陷阱選擇題與 2 題推導/問答題），並附上詳細詳解與評分標準。\n"
            "```"
        )

    conversation = f"初始提問：\n{issue.body}\n\n"
    for comment in issue.get_comments():
        conversation += f"[{comment.user.login} 回覆]：\n{comment.body}\n\n"

    contents = [
        f"目前的 Markdown 筆記內容：\n{current_note}\n\n",
        f"問答釐清紀錄：\n{conversation}\n\n",
        "請根據使用者的最新回覆，修正並補全筆記內容與摘要（務必保留所有原有的推導深度與 `assets/` 圖片標記）。"
        "請嚴格依照 ===SUMMARY===、===NOTES===、===NEXT_QUESTION=== 三個區塊標籤輸出。"
    ]

    if pdf_path and os.path.exists(pdf_path):
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
        contents.insert(0, types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"))

    config = types.GenerateContentConfig(
        system_instruction=UPDATE_SYSTEM_PROMPT,
        max_output_tokens=16384,
        temperature=0.2,
    )

    response = call_gemini_with_retry(contents=contents, config=config)
    data = parse_update_response(response.text)

    summary = fix_github_latex(data["summary"])
    notes = fix_github_latex(data["notes"])
    next_q = fix_github_latex(data["next_question"]) if data["next_question"] else None

    tz_tw = timezone(timedelta(hours=8))
    update_time_str = datetime.now(tz_tw).strftime("%Y-%m-%d %H:%M")

    # 組合最終 metadata 行（保留原始資訊並附上最後更新時間）
    if meta_line:
        clean_meta = re.sub(r"\s*｜\s*\*\*最後更新\*\*：.*$", "", meta_line)
        final_meta = f"{clean_meta} ｜ **最後更新**：`{update_time_str}`"
    else:
        final_meta = f"> **原始檔案**：`{pdf_path}` ｜ **最後更新**：`{update_time_str}`"

    final_content = (
        f"{first_line}\n"
        f"{final_meta}\n\n"
        f"## 📌 3 分鐘核心摘要\n{summary}\n\n"
        f"---\n\n"
        f"## 📖 結構化重點筆記（含自動剪貼圖表）\n{notes}\n\n"
        f"---\n\n"
        f"{gallery_section}"
        f"{prompt_section}\n"
    )

    with open(note_path, "w", encoding="utf-8") as f:
        f.write(final_content)
    print(f"✅ 已根據回覆更新筆記：{note_path}")

    if next_q:
        issue.create_comment(
            f"✅ 已將您的回覆整合至 `{note_path}`（更新時間：{update_time_str}）！\n\n"
            f"### 追問下一題：\n> **{next_q}**\n\n"
            f"請直接於下方繼續留言回覆。"
        )
    else:
        issue.create_comment(
            f"🎉 感謝確認！所有疑問皆已釐清，最終完整版筆記已更新至 `{note_path}`（更新時間：{update_time_str}），並已自動關閉此 Issue。"
        )
        issue.edit(state="closed")

if __name__ == "__main__":
    process_comment()
