import os
import re
import io
import glob
import json
import time
import pymupdf
from PIL import Image, ImageEnhance, ImageOps
from datetime import datetime, timezone, timedelta
from google import genai
from google.genai import types
from github import Github, Auth

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO_NAME = os.getenv("GITHUB_REPOSITORY")

if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY 未設定，請檢查 Secrets。")

client = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM_PROMPT = """
你是一位專業的高效學習助理與知識工程師。你的任務是閱讀使用者上傳的課堂板書/講義圖片，將其整理為條理分明、概念嚴謹的 Markdown 筆記。

請嚴格輸出合法的 JSON 格式（不要加任何其他說明文字），結構與欄位如下：
{
  "topic": "用 5~12 個字精簡概括這份板書的核心主題（做為檔案名稱與標題用，請勿包含斜線或特殊符號）",
  "summary": "3~5 個重點的核心精華摘要 (Markdown 格式)",
  "notes": "完整的結構化筆記。請在需要放入板書原圖表的位置插入 {{FIG_1}}、{{FIG_2}} 標記；若是方塊圖或流程圖，亦可搭配 ```mermaid 語法重繪。",
  "diagrams": [
    "頁碼|ymin,xmin,ymax,xmax|圖表標題說明|{{FIG_1}}"
  ],
  "question": "如果板書有缺漏、遮蔽、筆跡模糊或邏輯中斷，請提出『一個』最關鍵的具體問題向使用者確認。如果內容完整無缺漏，請填 null。"
}

【圖表剪貼規範】：
1. 若板書中含有重要的「電路圖、波形圖、相量圖、結構圖」，請在 `diagrams` 陣列中回傳字串，格式嚴格為：`頁碼|ymin,xmin,ymax,xmax|圖表標題|{{FIG_1}}`（頁碼從 1 開始，座標為 0~1000 整數比例），並在 `notes` 內文對應段落插入 `{{FIG_1}}`、`{{FIG_2}}`。若無圖表，`diagrams` 請填 `[]`。
2. 擷取座標時請稍微留邊距，確保座標軸文字、元件標號（如 $V_s, L_c, i_o$）完整包含在框內。

【GitHub LaTeX 公式排版嚴格規範（極重要）】：
1. 行內公式請使用單個錢字號 `$公式$`，獨立區塊公式請使用雙錢字號 `$$公式$$` 並前後空行。
2. 絕對禁止將 `$公式$` 緊貼半形括號或中文字，外側請務必各留一個半形空白（例如：`（ $0 \\sim t_a$ ）`）。
3. 區塊公式 `$$` 內部絕對不能有空行，且開頭與結尾的 `$$` 必須獨佔一行。
"""

def is_valid_pdf(filepath: str) -> bool:
    if not os.path.exists(filepath) or os.path.getsize(filepath) < 100:
        return False
    try:
        doc = pymupdf.open(filepath)
        page_count = len(doc)
        doc.close()
        return page_count > 0
    except Exception:
        return False

def sanitize_filename(name: str) -> str:
    """清除不能當作檔案名稱的特殊字元（如 / \\ : * ? " < > | 與多餘空白）"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', '_', name.strip())
    cleaned = re.sub(r'\s+', '_', cleaned)
    return cleaned or "課堂筆記"

def enhance_board_image(pil_img: Image.Image) -> Image.Image:
    img = pil_img.convert("RGB")
    img = ImageOps.autocontrast(img, cutoff=1)
    img = ImageEnhance.Contrast(img).enhance(1.25)
    img = ImageEnhance.Sharpness(img).enhance(1.5)
    return img

def render_pdf_pages(pdf_path: str, asset_dir: str, prefix: str):
    os.makedirs(asset_dir, exist_ok=True)
    doc = pymupdf.open(pdf_path)
    page_images = {}
    full_page_md_list = []
    api_image_parts = []

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        pix = page.get_pixmap(matrix=pymupdf.Matrix(2.0, 2.0), alpha=False)
        pil_img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        opt_img = enhance_board_image(pil_img)
        page_num = page_idx + 1
        page_images[page_num] = opt_img

        page_filename = f"{prefix}_page_{page_num}.png"
        page_save_path = os.path.join(asset_dir, page_filename)
        opt_img.save(page_save_path, "PNG", optimize=True)
        full_page_md_list.append((page_num, f"assets/{page_filename}"))

        buf = io.BytesIO()
        opt_img.save(buf, format="JPEG", quality=85)
        api_image_parts.append(f"【第 {page_num} 頁板書圖片】：")
        api_image_parts.append(types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg"))

    doc.close()
    return page_images, full_page_md_list, api_image_parts

def crop_diagrams(page_images: dict, asset_dir: str, prefix: str, diagrams: list):
    fig_markdown_map = {}
    if not isinstance(diagrams, list):
        return fig_markdown_map

    for idx, item in enumerate(diagrams, start=1):
        try:
            if not isinstance(item, str):
                continue
            parts = [p.strip() for p in item.split("|")]
            if len(parts) < 3:
                continue
            p_num = int(re.sub(r'\D', '', parts[0]) or "1")
            coords = [int(float(x.strip())) for x in parts[1].split(",")]
            caption = parts[2]
            placeholder_id = parts[3] if len(parts) >= 4 else f"{{{{FIG_{idx}}}}}"

            if p_num not in page_images or len(coords) != 4:
                continue

            base_img = page_images[p_num]
            w, h = base_img.size
            ymin, xmin, ymax, xmax = coords

            ymin = max(0, ymin - 15)
            xmin = max(0, xmin - 15)
            ymax = min(1000, ymax + 15)
            xmax = min(1000, xmax + 15)

            left = int((xmin / 1000.0) * w)
            top = int((ymin / 1000.0) * h)
            right = int((xmax / 1000.0) * w)
            bottom = int((ymax / 1000.0) * h)

            if right > left + 20 and bottom > top + 20:
                cropped = base_img.crop((left, top, right, bottom))
                fig_filename = f"{prefix}_fig_{idx}.png"
                fig_save_path = os.path.join(asset_dir, fig_filename)
                cropped.save(fig_save_path, "PNG", optimize=True)

                rel_img_path = f"assets/{fig_filename}"
                md_snippet = (
                    f"\n\n> 🖼️ **{caption}**（擷取自第 {p_num} 頁）\n"
                    f"> ![{caption}]({rel_img_path})\n\n"
                )
                fig_markdown_map[placeholder_id] = md_snippet
        except Exception as e:
            print(f"⚠️ 略過無法解析的圖表標記 ({item}): {e}")

    return fig_markdown_map

def fix_github_latex(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\t", "\\t").replace("\b", "\\b").replace("\f", "\\f").replace("\r", "");

    def format_block_math(match):
        content = match.group(1).strip()
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        clean_content = "\n".join(lines)
        return f"\n\n$$\n{clean_content}\n$$\n\n"

    text = re.sub(r'\$\$([\s\S]+?)\$\$', format_block_math, text);

    def format_inline_math(match):
        inner = match.group(1).strip()
        return f" ${inner}$ "

    text = re.sub(r'(?<!\$)\$(?!\$)([^\$\n]+?)(?<!\$)\$(?!\$)', format_inline_math, text);
    text = re.sub(r'[ ]{2,}', ' ', text);
    text = re.sub(r'\n{3,}', '\n\n', text);
    return text.strip()

def safe_parse_json(raw_text: str) -> dict:
    cleaned = raw_text.strip()
    cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
    cleaned = re.sub(r'\s*```$', '', cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        fixed = re.sub(r'\\(?![/\"\\bfnrtu])', r'\\\\', cleaned)
        return json.loads(fixed)

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
    all_pdfs = glob.glob("原始檔案/**/*.pdf", recursive=True) + glob.glob("incoming/**/*.pdf", recursive=True)
    
    valid_pdfs = []
    for p in all_pdfs:
        if is_valid_pdf(p):
            valid_pdfs.append(p)
        else:
            print(f"⚠️ 略過無效或空白的檔案: {p}")

    if not valid_pdfs:
        print("❌ 未找到任何有效的 PDF 檔案！請使用 Add file -> Upload files 上傳真正的 PDF。")
        return

    latest_pdf = max(valid_pdfs, key=os.path.getctime)
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

    date_match = re.search(r'20\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])', latest_pdf)
    if date_match:
        date_str = date_match.group(0)
        target_date = datetime.strptime(date_str, "%Y-%m-%d")
    else:
        tz_tw = timezone(timedelta(hours=8))
        target_date = datetime.now(tz_tw)
        date_str = target_date.strftime("%Y-%m-%d")

    weekdays = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    weekday_str = weekdays[target_date.weekday()]

    output_dir = f"notes/{subject}"
    asset_dir = f"{output_dir}/assets"
    os.makedirs(asset_dir, exist_ok=True)

    print(f"✅ 正在處理有效檔案: {latest_pdf} (科目: {subject}, 日期: {date_str})")

    # 圖片檔名前綴使用日期與原檔名雜湊，避免重複
    img_prefix = f"{date_str}_{sanitize_filename(base_name)}"
    page_images, full_pages, api_image_parts = render_pdf_pages(latest_pdf, asset_dir, img_prefix)

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
    )

    prompt_text = (
        f"以上是「{subject}」科目於 {date_str} 的板書/講義（共 {len(page_images)} 頁）。"
        "請詳細閱讀並依照 JSON 規範產出 topic（精準題目名稱）、summary、notes、diagrams（局部電路/波形圖座標）與 question。"
    )

    response = call_gemini_with_retry(
        contents=api_image_parts + [prompt_text],
        config=config
    )

    data = safe_parse_json(response.text)
    topic = data.get("topic", "課堂重點整理").strip()
    summary = data.get("summary", "")
    notes = data.get("notes", "")
    diagrams = data.get("diagrams", [])
    question = data.get("question")

    # 🔥 關鍵修改：使用「日期 + AI 生成的題目 (topic)」作為最終輸出的 Markdown 檔名！
    safe_topic = sanitize_filename(topic)
    output_md_path = f"{output_dir}/{date_str}_{safe_topic}.md"

    # 若之前有殘留「日期_日期.md」的舊檔，順便自動清理掉，讓資料夾保持乾淨
    old_dup_path = f"{output_dir}/{date_str}_{base_name}.md"
    if old_dup_path != output_md_path and os.path.exists(old_dup_path):
        try:
            os.remove(old_dup_path)
            print(f"🧹 已自動清理舊檔名檔案: {old_dup_path}")
        except Exception:
            pass

    fig_map = crop_diagrams(page_images, asset_dir, img_prefix, diagrams)
    for placeholder, md_img in fig_map.items():
        if placeholder in notes:
            notes = notes.replace(placeholder, md_img)
        else:
            notes += f"\n{md_img}"

    notes = re.sub(r'\{\{FIG_\d+\}\}', '', notes)

    summary = fix_github_latex(summary)
    notes = fix_github_latex(notes)
    if question:
        question = fix_github_latex(str(question))

    pages_gallery_md = "\n".join([f"#### 📄 第 {p_num} 頁\n![第 {p_num} 頁]({p_path})\n" for p_num, p_path in full_pages])
    gallery_section = (
        "## 🖼️ 原始板書/講義高畫質對照（已自動去陰影與銳利化）\n"
        "<details>\n"
        "<summary>👉 點此展開查看完整板書原圖（共 " + str(len(full_pages)) + " 頁）</summary>\n\n"
        f"{pages_gallery_md}\n"
        "</details>\n"
    )

    header_title = f"# 📅 {date_str} ({weekday_str})｜【{subject}】{topic}"

    prompt_block = (
        "```markdown\n"
        f"你現在是「{subject}」領域的教學專家。請根據上述「{topic}」的筆記內容，出 3 題觀念驗證題（包含 1 題陷阱選擇題與 2 題推導/問答題），並附上詳細詳解與評分標準。\n"
        "```"
    )

    final_content = (
        f"{header_title}\n"
        f"> **科目**：`{subject}` ｜ **上課日期**：`{date_str}` ｜ **原始檔案**：`{latest_pdf}`\n\n"
        f"## 📌 3 分鐘核心摘要\n{summary}\n\n"
        f"---\n\n"
        f"## 📖 結構化重點筆記（含自動剪貼圖表）\n{notes}\n\n"
        f"---\n\n"
        f"{gallery_section}\n"
        f"---\n\n"
        f"## 💡 後續複習提示詞 (Prompt)\n{prompt_block}\n"
    )

    with open(output_md_path, "w", encoding="utf-8") as f:
        f.write(final_content)
    print(f"✅ 筆記與優化圖片已成功寫入 {output_md_path}")

    if question and str(question).strip().lower() not in ("null", "none", ""):
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
