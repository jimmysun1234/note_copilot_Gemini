import os
import re
import glob
import json
import time
from datetime import datetime, timezone, timedelta
from pydantic import BaseModel, Field
from typing import Optional
from google import genai
from google.genai import types
from github import Github, Auth

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO_NAME = os.getenv("GITHUB_REPOSITORY")

if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY 未設定，請檢查 Secrets。")

client = genai.Client(api_key=GEMINI_API_KEY)

class NoteOutput(BaseModel):
    topic: str = Field(description="用 5~12 個字精簡概括這份板書的核心主題（做為標題用）")
    summary: str = Field(description="3~5 個重點的核心精華摘要 (Markdown 格式)")
    notes: str = Field(description="完整的結構化筆記，包含章節、公式推導、關鍵觀念、範例 (Markdown 格式)")
    question: Optional[str] = Field(default=None, description="如果板書有缺漏、遮蔽、筆跡模糊或邏輯中斷，請提出『一個』最關鍵的具體問題向使用者確認。如果內容完整無缺漏，請填 null。")

SYSTEM_PROMPT = """
你是一位專業的高效學習助理與知識工程師。你的任務是閱讀使用者上傳的課堂板書/筆記 PDF，將其整理為條理分明、概念嚴謹的 Markdown 筆記。

【GitHub LaTeX 公式排版嚴格規範（極重要）】：
1. 行內公式請使用單個錢字號 `$公式$`，獨立區塊公式請使用雙錢字號 `$$公式$$` 並前後空行。
2. 絕對禁止將 `$公式$` 放在半形括號 `(...)` 內緊貼，若需括號請一律使用全形中文括號 `（$公式$）`，或在括號內側留空白 `( $公式$ )`。
3. `$` 符號的內側開頭與結尾絕對不可有空格（正確：`$L \\to \\infty$`，錯誤：`$ L \\to \\infty $`）。
4. 嚴禁在 Markdown 標題（`#`, `##`, `###`）或粗體（`**...**`）內部嵌套 `$公式$`，以免 GitHub 渲染失效。
"""

def fix_github_latex(text: str) -> str:
    """自動修正 GitHub Markdown 的 LaTeX 渲染地雷，確保 100% 正常顯示"""
    if not text:
        return ""
    
    # 1. 修復被 JSON 誤轉義的控制字元（例如 \to 的 \t 被變成 tab, \beta 的 \b 被變成退格, \frac 的 \f 被變成換頁）
    text = text.replace("\t", "\\t").replace("\b", "\\b").replace("\f", "\\f")
    
    # 2. 修正行內公式內側多餘空白：將 $xxx$ 修正為 $xxx$
    def strip_inner_math(match):
        inner = match.group(1).strip()
        return f"${inner}$"
    text = re.sub(r'(?<!\$)\$(?!\$)([^\$\n]+?)(?<!\$)\$(?!\$)', strip_inner_math, text)

    # 3. 解決半形括號緊貼 $ 導致 GitHub 無法渲染的問題：將 ($...$) 轉為 （$...$） 或補空白
    text = re.sub(r'\((\$[^\$\n]+\$)\)', r'（\1）', text)
    text = re.sub(r'\((\$[^\$\n]+\$)', r'( \1', text) text = re.sub(r'(\$[^\$\n]+\$)\)', r'\1 )', text)

    # 4. 確保獨立公式區塊 $$ 前後都有換行
    text = re.sub(r'([^\n])\s*\$\$([\s\S]+?)\$\$', r'\1\n\n$$\2$$', text)
    text = re.sub(r'\$\$([\s\S]+?)\$\$\s*([^\n])', r'$$\1$$\n\n\2', text)

    return text

def call_gemini_with_retry(contents, config, max_retries=5):
    for attempt in range(1, max_retries + 1):
        try:
            return client.models.generate_content(
                model="gemini-3.8-flash",
                contents=contents,
                config=config
            )
        except Exception as e:
            err_msg = str(e)
            if ("503" in err_msg or "429" in err_msg or "UNAVAILABLE" in err_msg) and attempt < max_retries:
                wait_sec = attempt * 10
                print(f"⚠️ Google 伺服器忙碌中 (第 {attempt}/{max_retries} 次嘗試)，等待 {wait_sec} 秒後重試...")
                time.sleep(wait_sec)
            else:
                raise e

def process_latest_pdf():
    pdf_files = glob.glob("原始檔案/**/*.pdf", recursive=True) + glob.glob("incoming/**/*.pdf", recursive=True)
    if not pdf_files:
        print("未在 原始檔案/ 或 incoming/ 找到任何 PDF 檔案。")
        return

    latest_pdf = max(pdf_files, key=os.path.getctime)
    pdf_name = os.path.basename(latest_pdf)
    base_name = os.path.splitext(pdf_name)[0]

    root_folder = "原始檔案" if latest_pdf.startswith("原始檔案") else "incoming"
    rel_dir = os.path.dirname(os.path.relpath(latest_pdf, root_folder))
    if rel_dir and rel_dir != ".":
        subject = rel_dir.split(os.sep)[0]
    elif "_" in base_name:
        subject = base_name.split("_")[0]
    else:
        subject = "一般科目"

    tz_tw = timezone(timedelta(hours=8))
    now_tw = datetime.now(tz_tw)
    date_str = now_tw.strftime("%Y-%m-%d")
    time_str = now_tw.strftime("%H:%M")
    weekdays = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    weekday_str = weekdays[now_tw.weekday()]

    output_dir = f"notes/{subject}"
    os.makedirs(output_dir, exist_ok=True)
    output_md_path = f"{output_dir}/{date_str}_{base_name}.md"

    print(f"正在處理檔案: {latest_pdf} (科目: {subject})")

    with open(latest_pdf, "rb") as f:
        pdf_bytes = f.read()

    pdf_part = types.Part.from_bytes(
        data=pdf_bytes,
        mime_type="application/pdf"
    )

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=NoteOutput,
    )

    response = call_gemini_with_retry(
        contents=[
            pdf_part,
            f"這是一份「{subject}」科目的板書 PDF，請詳細閱讀並依照規範產出核心主題、摘要、完整結構化筆記與單題缺漏提問。請特別注意 LaTeX 公式不要緊貼半形括號。"
        ],
        config=config
    )

    if response.parsed:
        result = response.parsed
        topic = result.topic
        summary = result.summary
        notes = result.notes
        question = result.question
    else:
        data = json.loads(response.text)
        topic = data.get("topic", base_name)
        summary = data.get("summary", "")
        notes = data.get("notes", "")
        question = data.get("question")

    # 執行 LaTeX 公式自動校正，確保渲染無誤
    summary = fix_github_latex(summary)
    notes = fix_github_latex(notes)
    if question:
        question = fix_github_latex(question)

    header_title = f"# 📅 {date_str} ({weekday_str}) {time_str}｜【{subject}】{topic}"

    prompt_block = (
        "```markdown\n"
        f"你現在是「{subject}」領域的教學專家。請根據上述「{topic}」的筆記內容，出 3 題觀念驗證題（包含 1 題陷阱選擇題與 2 題推導/問答題），並附上詳細詳解與評分標準。\n"
        "```"
    )

    final_content = (
        f"{header_title}\n"
        f"> **科目**：`{subject}` ｜ **原始檔案**：`{latest_pdf}`\n\n"
        f"## 📌 3 分鐘核心摘要\n{summary}\n\n"
        f"---\n\n"
        f"## 📖 結構化重點筆記\n{notes}\n\n"
        f"---\n\n"
        f"## 💡 後續複習提示詞 (Prompt)\n{prompt_block}\n"
    )

    with open(output_md_path, "w", encoding="utf-8") as f:
        f.write(final_content)
    print(f"✅ 筆記已成功寫入 {output_md_path}")

    if question and str(question).strip().lower() != "null":
        try:
            auth = Auth.Token(GITHUB_TOKEN)
            gh = Github(auth=auth)
            repo = gh.get_repo(REPO_NAME)
            issue_title = f"【{subject}｜筆記待補全】{date_str} {topic}"
            issue_body = (
                "### AI 學習助理提問\n"
                f"在閱讀 `{latest_pdf}` 時發現部分內容需釐清：\n\n"
                f"> **{question}**\n\n"
                "---\n"
                f"💡 **回覆說明**：直接在此 Issue 留言回答，補全資訊將會在後續整合至 `{output_md_path}`。\n"
            )
            repo.create_issue(title=issue_title, body=issue_body)
            print(f"✅ 已針對缺漏建立 Issue 發問: {issue_title}")
        except Exception as e:
            print(f"⚠️ 建立 Issue 時發生警告（不影響筆記儲存）: {e}")

if __name__ == "__main__":
    process_latest_pdf()
