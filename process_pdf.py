import os
import re
import glob
import json
import time
import fitz  # PyMuPDF
from PIL import Image, ImageEnhance, ImageOps
from datetime import datetime, timezone, timedelta
from pydantic import BaseModel, Field
from typing import Optional, List
from google import genai
from google.genai import types
from github import Github, Auth

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO_NAME = os.getenv("GITHUB_REPOSITORY")

if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY 未設定，請檢查 Secrets。")

client = genai.Client(api_key=GEMINI_API_KEY)

class DiagramCrop(BaseModel):
    page: int = Field(description="圖表所在的 PDF 頁碼（從 1 開始計算）")
    box_2d: List[int] = Field(description="圖表區域的邊界框座標 [ymin, xmin, ymax, xmax]，數值範圍為 0 到 1000 的整數")
    caption: str = Field(description="該圖表的精確標題與說明（例如：半波整流含漏感等效電路圖與換相波形）")
    placeholder_id: str = Field(description="對應在 notes 內文中插入位置的標記，格式固定為 {{FIG_1}}、{{FIG_2}} 依此類推")

class NoteOutput(BaseModel):
    topic: str = Field(description="用 5~12 個字精簡概括這份板書的核心主題（做為標題用）")
    summary: str = Field(description="3~5 個重點的核心精華摘要 (Markdown 格式)")
    notes: str = Field(description="完整的結構化筆記。請在需要放原圖表的位置插入對應的 {{FIG_1}}、{{FIG_2}} 標記；若是方塊圖或流程圖，亦可搭配 ```mermaid 語法重繪。")
    diagrams: List[DiagramCrop] = Field(default=[], description="從 PDF 中擷取出的關鍵電路圖、波形圖或推導圖表清單")
    question: Optional[str] = Field(default=None, description="如果板書有缺漏、遮蔽、筆跡模糊或邏輯中斷，請提出『一個』最關鍵的具體問題向使用者確認。如果內容完整無缺漏，請填 null。")

SYSTEM_PROMPT = """
你是一位專業的高效學習助理與知識工程師。你的任務是閱讀使用者上傳的課堂板書/筆記 PDF，將其整理為條理分明、概念嚴謹的 Markdown 筆記。

【圖表剪貼與視覺化規範】：
1. 若板書或講義中含有重要的「電路圖、波形圖、相量圖、結構圖」，請在 `diagrams` 欄位回傳其頁碼與 `box_2d` 座標 `[ymin, xmin, ymax, xmax]`（0~1000 比例），並在 `notes` 內文最合適的段落處插入對應的 `{{FIG_1}}`、`{{FIG_2}}` 標記，系統會自動將該區域剪貼成高清圖片嵌入筆記中。
2. 擷取 `box_2d` 時請稍微留一點邊距，確保座標軸文字、元件標號（如 $V_s, L_c, i_o$）完整包含在框內。
3. 若內容包含控制方塊圖、系統架構或演算法流程，也歡迎在筆記中使用 GitHub 支援的 ```mermaid 語法繪製清晰圖表輔助說明。

【GitHub LaTeX 公式排版嚴格規範（極重要）】：
1. 行內公式請使用單個錢字號 `$公式$`，獨立區塊公式請使用雙錢字號 `$$公式$$` 並前後空行。
2. 絕對禁止將 `$公式$` 緊貼半形括號或中文字，外側請務必各留一個半形空白（例如：`（ $0 \\sim t_a$ ）`）。
3. 區塊公式 `$$` 內部絕對不能有空行，且開頭與結尾的 `$$` 必須獨佔一行。
"""

def enhance_board_image(pil_img: Image.Image) -> Image.Image:
    """優化板書與講義圖片：提升解析對比度、銳利化字跡與線條"""
    img = pil_img.convert("RGB")
    # 自動色階調整（去除灰暗背景）
    img = ImageOps.autocontrast(img, cutoff=1)
    # 提高對比度讓筆跡與電路圖線條更清晰
    img = ImageEnhance.Contrast(img).enhance(1.25)
    # 提高銳利度讓小字與下標更清楚
    img = ImageEnhance.Sharpness(img).enhance(1.5)
    return img

def extract_and_optimize_images(pdf_path: str, asset_dir: str, prefix: str, diagrams: List[DiagramCrop]):
    """從 PDF 渲染高清頁面與局部裁切圖表，並回傳替換字典與全頁預覽清單"""
    os.makedirs(asset_dir, exist_ok=True)
    doc = fitz.open(pdf_path)
    fig_markdown_map = {}
    full_page_md_list = []

    # 1. 將每一頁存成高清優化圖（2x zoom 約 150-200 DPI，兼顧畫質與載入速度）
    page_images = {}
    for page_idx in range(len(doc)):
        page = doc[page_idx]
        pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
        pil_img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        opt_img = enhance_board_image(pil_img)
        page_images[page_idx + 1] = opt_img

        page_filename = f"{prefix}_page_{page_idx + 1}.png"
        page_save_path = os.path.join(asset_dir, page_filename)
        opt_img.save(page_save_path, "PNG", optimize=True)
        full_page_md_list.append((page_idx + 1, f"assets/{page_filename}"))

    # 2. 針對 AI 標記的局部電路圖/波形圖進行精準剪貼與增強
    for idx, diag in enumerate(diagrams, start=1):
        p_num = diag.page
        if p_num not in page_images or len(diag.box_2d) != 4:
            continue
        base_img = page_images[p_num]
        w, h = base_img.size
        ymin, xmin, ymax, xmax = diag.box_2d

        # 加入 2% 安全邊距避免切到標號
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
                f"\n\n> 🖼️ **{diag.caption}**（擷取自第 {p_num} 頁）\n"
                f"> ![{diag.caption}]({rel_img_path})\n\n"
            )
            fig_markdown_map[diag.placeholder_id] = md_snippet

    doc.close()
    return fig_markdown_map, full_page_md_list

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

    # 1. 自動辨識科目名稱
    root_folder = "原始檔案" if latest_pdf.startswith("原始檔案") else "incoming"
    rel_dir = os.path.dirname(os.path.relpath(latest_pdf, root_folder))
    if rel_dir and rel_dir != ".":
        subject = rel_dir.split(os.sep)[0]
    elif "_" in base_name:
        subject = base_name.split("_")[0]
    else:
        subject = "一般科目"

    # 2. 智慧抓取日期
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
    output_md_path = f"{output_dir}/{date_str}_{base_name}.md"

    print(f"正在處理檔案: {latest_pdf} (科目: {subject}, 日期: {date_str})")

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
            f"這是一份「{subject}」科目於 {date_str} 的板書/講義 PDF，請詳細閱讀並產出核心主題、摘要、完整結構化筆記、關鍵圖表裁切座標（diagrams）與單題缺漏提問。"
        ],
        config=config
    )

    if response.parsed:
        result = response.parsed
        topic = result.topic
        summary = result.summary
        notes = result.notes
        diagrams = result.diagrams or []
        question = result.question
    else:
        data = json.loads(response.text)
        topic = data.get("topic", base_name)
        summary = data.get("summary", "")
        notes = data.get("notes", "")
        diagrams = [DiagramCrop(**d) for d in data.get("diagrams", [])]
        question = data.get("question")

    # 3. 執行 PDF 圖片提取、自動去背增晰與局部圖表剪貼
    prefix = f"{date_str}_{base_name}"
    fig_map, full_pages = extract_and_optimize_images(latest_pdf, asset_dir, prefix, diagrams)

    # 將筆記中的 {{FIG_X}} 標記替換為實際裁切並優化後的圖片連結
    for placeholder, md_img in fig_map.items():
        if placeholder in notes:
            notes = notes.replace(placeholder, md_img)
        else:
            notes += f"\n{md_img}"

    # 清除未被替換到的殘留標記
    notes = re.sub(r'\{\{FIG_\d+\}\}', '', notes)

    summary = fix_github_latex(summary)
    notes = fix_github_latex(notes)
    if question:
        question = fix_github_latex(question)

    # 4. 建立底部「原始板書高畫質逐頁對照」摺疊區塊（點開即可對照原圖）
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
