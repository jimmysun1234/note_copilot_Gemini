import os
import re
import glob
import json
import time
import pymupdf
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

# 採用單層扁平化 Schema，無嵌套類別、無 default 複雜預設值，100% 符合 Gemini API 規範
class NoteOutput(BaseModel):
    topic: str = Field(description="用 5~12 個字精簡概括這份板書的核心主題（做為標題用）")
    summary: str = Field(description="3~5 個重點的核心精華摘要 (Markdown 格式)")
    notes: str = Field(description="完整的結構化筆記。請在需要放原圖表的位置插入對應的 {{FIG_1}}、{{FIG_2}} 標記；若是方塊圖或流程圖，亦可搭配 ```mermaid 語法重繪。")
    diagrams: List[str] = Field(description="從 PDF 擷取的關鍵電路圖或波形圖清單。每個元素格式必須嚴格為：'頁碼|ymin,xmin,ymax,xmax|圖表說明|{{FIG_X}}'，例如 '1|150,100,550,900|半波整流電路與波形|{{FIG_1}}'。座標範圍 0~1000。若無圖表請回傳空陣列 []。")
    question: Optional[str] = Field(description="如果板書有缺漏、遮蔽、筆跡模糊或邏輯中斷，請提出『一個』最關鍵的具體問題向使用者確認。如果內容完整無缺漏，請填 null。")

SYSTEM_PROMPT = """
你是一位專業的高效學習助理與知識工程師。你的任務是閱讀使用者上傳的課堂板書/筆記 PDF，將其整理為條理分明、概念嚴謹的 Markdown 筆記。

【圖表剪貼與視覺化規範】：
1. 若板書或講義中含有重要的「電路圖、波形圖、相量圖、結構圖」，請在 `diagrams` 陣列中回傳字串，格式嚴格為：`頁碼|ymin,xmin,ymax,xmax|圖表標題|{{FIG_1}}`（座標為 0~1000 整數比例），並在 `notes` 內文最合適的段落處插入對應的 `{{FIG_1}}`、`{{FIG_2}}` 標記。
2. 擷取座標時請稍微留一點邊距，確保座標軸文字、元件標號（如 $V_s, L_c, i_o$）完整包含在框內。
3. 若內容包含控制方塊圖或流程，也歡迎在筆記中使用 GitHub 支援的 ```mermaid 語法繪製清晰圖表輔助說明。

【GitHub LaTeX 公式排版嚴格規範（極重要）】：
1. 行內公式請使用單個錢字號 `$公式$`，獨立區塊公式請使用雙錢字號 `$$公式$$` 並前後空行。
2. 絕對禁止將 `$公式$` 緊貼半形括號或中文字，外側請務必各留一個半形空白（例如：`（ $0 \\sim t_a$ ）`）。
3. 區塊公式 `$$` 內部絕對不能有空行，且開頭與結尾的 `$$` 必須獨佔一行。
"""

def enhance_board_image(pil_img: Image.Image) -> Image.Image:
    """優化板書與講義圖片：提升解析對比度、銳利化字跡與線條"""
    img = pil_img.convert("RGB")
    img = ImageOps.autocontrast(img, cutoff=1)
    img = ImageEnhance.Contrast(img).enhance(1.25)
    img = ImageEnhance.Sharpness(img).enhance(1.5)
    return img

def extract_and_optimize_images(pdf_path: str, asset_dir: str, prefix: str, diagrams: List[str]):
    """從 PDF 渲染高清頁面與局部裁切圖表，並回傳替換字典與全頁預覽清單"""
    os.makedirs(asset_dir, exist_ok=True)
    doc = pymupdf.open(pdf_path)
    fig_markdown_map = {}
    full_page_md_list = []

    # 1. 將每一頁存成高清優化圖
    page_images = {}
    for page_idx in range(len(doc)):
        page = doc[page_idx]
        pix = page.get_pixmap(matrix=pymupdf.Matrix(2.0, 2.0), alpha=False)
        pil_img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        opt_img = enhance_board_image(pil_img)
        page_images[page_idx + 1] = opt_img

        page_filename = f"{prefix}_page_{page_idx + 1}.png"
        page_save_path = os.path.join(asset_dir, page_filename)
        opt_img.save(page_save_path, "PNG", optimize=True)
        full_page_md_list.append((page_idx + 1, f"assets/{page_filename}"))

    # 2. 解析字串並針對局部電路圖/波形圖進行精準剪貼
    for idx, item in enumerate(diagrams, start=1):
        try:
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
    output_md_path = f"{output_dir}/{date_str}_{base_name
