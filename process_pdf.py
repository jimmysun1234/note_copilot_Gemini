import os
import re
import io
import glob
import hashlib
import pymupdf
from PIL import Image, ImageEnhance, ImageOps
from datetime import datetime, timezone, timedelta
from pathlib import Path
from google import genai
from google.genai import errors, types
from github import Github, Auth

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO_NAME = os.getenv("GITHUB_REPOSITORY")

SYSTEM_PROMPT = """
你是一位專精於「工程與理工科目（特別是電力電子、控制系統、電路學）」的大學助教與知識工程師。
你的任務是將使用者上傳的課堂板書/講義圖片，整理成一份**極度詳盡、完整保留數學推導與物理直覺的教科書級 Markdown 筆記**（目標長度至少 1500～3000 字，絕不偷工減料）。

【核心寫作鐵則（務必嚴格遵守）】
1. **禁止大綱式跳躍**：嚴禁只寫「經由伏秒平衡可得某式」或「整理得二次方程式」這類跳過過程的摘要！凡是板書上出現的公式推導，你必須補齊**所有中間代數化簡步驟**（Step 1, Step 2, Step 3...），並說明每一步使用的物理定律（如 KVL、KCL、電感伏秒平衡、電容安培秒平衡、三角形面積法求平均電流等）。
2. **符號嚴格對齊板書**：完全尊重板書上的符號定義（例如板書若將導通區間記為 $D_1 T$ 、續流區間記為 $D_2 T$ 、截止區間記為 $D_3 T$ ，內文與公式就必須統一使用 $D_1, D_2, D_3$ ，不可擅自改寫）。
3. **圖表深度解讀**：在插入 `{{FIG_1}}`、`{{FIG_2}}` 標記之後，**必須緊接著寫一段 3～5 句話的「圖表與波形解析」**，詳細說明座標軸、關鍵轉折點、斜率變化與背後物理意義。
4. **涵蓋板書所有角落**：仔細掃描每一頁板書的邊角補充（例如：臨界參數 $K_{\\text{crit}}$ 與臨界電阻 $R_{\\text{crit}}$ 的雙視角比較、不同模式下的 RMS 電流公式推導、多相並聯或 ESR 導通損耗比較等），確保無任何遺漏。

請嚴格依照以下 5 個區塊標籤格式輸出（請務必保留 ===標籤=== 字樣，不要輸出 JSON）：

===TOPIC===
用 5~12 個字精簡概括這份板書的核心主題（做為檔案名稱與標題用，請勿包含斜線或特殊符號）

===SUMMARY===
4~6 個重點的核心精華摘要（Markdown 格式，需點出核心公式與物理結論）

===NOTES===
完整的結構化深度筆記（Markdown 格式）。
請按邏輯拆分為多個大章節（`## 1. ...`、`## 2. ...`），每個章節底下視需要包含：
- **核心概念與電路模態分析**（逐一列出各時間區段內開關/二極體的導通狀態、電感電壓 $v_L(t)$ 與電容電流 $i_C(t)$ 表示式）
- **完整數學公式推導（Step-by-Step）**（一步一步展開代數消去與化簡過程）
- **關鍵波形與圖表解析**（插入 `{{FIG_1}}`、`{{FIG_2}}` 等標記並附上詳細文字解說）
- **物理意義、邊界條件與損耗比較**（整理成 Markdown 比較表格或條列分析）
- **考點與實務設計提醒**

===DIAGRAMS===
從板書擷取的關鍵電路圖、特性曲線或波形對比圖清單（請盡可能完整擷取板書上的所有手繪圖表，通常每份板書有 2~4 張圖），每行一個，格式嚴格為：
頁碼|ymin,xmin,ymax,xmax|圖表標題說明|{{FIG_1}}
（例如：1|150,100,550,900|Buck轉換器電路與CCM/DCM電感電流波形|{{FIG_1}}，座標為 0~1000 整數比例。若無圖表請寫 NONE）

===QUESTION===
如果板書有缺漏、遮蔽、筆跡模糊或邏輯中斷，請提出『一個』最關鍵的具體問題向使用者確認。如果內容完整無缺漏，請寫 NONE。

【GitHub LaTeX 公式排版嚴格規範（極重要）】：
1. 行內公式請使用單個錢字號 `$公式$`，獨立區塊公式請使用雙錢字號 `$$公式$$` 並前後空行。
2. 絕對禁止將 `$公式$` 緊貼半形括號或中文字，外側請務必各留一個半形空白（例如：`（ $0 \\sim D_1 T$ ）`）。
3. 區塊公式 `$$` 內部絕對不能有空行，且開頭與結尾的 `$$` 必須獨佔一行。
"""

def parse_tagged_response(text: str) -> dict:
    def extract_section(tag_name, next_tags):
        next_pattern = "|".join([f"==={t}===" for t in next_tags])
        pattern = rf"==={tag_name}===\s*([\s\S]*?)(?=(?:{next_pattern})|$)"
        match = re.search(pattern, text)
        return match.group(1).strip() if match else ""

    topic = extract_section("TOPIC", ["SUMMARY", "NOTES", "DIAGRAMS", "QUESTION"])
    summary = extract_section("SUMMARY", ["NOTES", "DIAGRAMS", "QUESTION"])
    notes = extract_section("NOTES", ["DIAGRAMS", "QUESTION"])
    diagrams_raw = extract_section("DIAGRAMS", ["QUESTION"])
    question = extract_section("QUESTION", [])

    if not topic or not summary or not notes:
        raise ValueError("Gemini 回覆缺少標題、摘要或正文，未建立筆記。")

    topic = topic.splitlines()[0].strip()

    diagrams = []
    if diagrams_raw and diagrams_raw.upper() != "NONE":
        for line in diagrams_raw.splitlines():
            line = line.strip().lstrip("-* ")
            if "|" in line and "FIG_" in line:
                diagrams.append(line)

    if question.upper() in ("NONE", "NULL", ""):
        question = None

    return {
        "topic": topic,
        "summary": summary,
        "notes": notes,
        "diagrams": diagrams,
        "question": question
    }

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
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t$`]+', '', name.strip())
    cleaned = re.sub(r'\s+', '_', cleaned)
    return cleaned[:40] or "課堂筆記"

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
        # 提升渲染解析度至 2.5x，讓手寫公式下標（如 D1, D2, K_crit）更清晰
        pix = page.get_pixmap(matrix=pymupdf.Matrix(2.5, 2.5), alpha=False)
        pil_img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        opt_img = enhance_board_image(pil_img)
        page_num = page_idx + 1
        page_images[page_num] = opt_img

        page_filename = f"{prefix}_page_{page_num}.png"
        page_save_path = os.path.join(asset_dir, page_filename)
        opt_img.save(page_save_path, "PNG", optimize=True)
        full_page_md_list.append((page_num, f"assets/{page_filename}"))

        buf = io.BytesIO()
        opt_img.save(buf, format="JPEG", quality=92)
        api_image_parts.append(f"【第 {page_num} 頁板書圖片】：")
        api_image_parts.append(types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg"))

    doc.close()
    return page_images, full_page_md_list, api_image_parts

def crop_diagrams(page_images: dict, asset_dir: str, prefix: str, diagrams: list):
    fig_markdown_map = {}
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

    return fig_markdown_map

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
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY 未設定，請檢查 Secrets。")

    def generate(model, attempts):
        http_options = types.HttpOptions(
            timeout=180_000,  # 延長超時至 180 秒，確保長篇推導不會中斷
            retry_options=types.HttpRetryOptions(
                attempts=attempts,
                initial_delay=2,
                max_delay=10,
                exp_base=2,
                jitter=1,
                http_status_codes=[408, 500, 502, 503, 504],
            ),
        )
        with genai.Client(api_key=api_key, http_options=http_options) as client:
            return client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )

    try:
        return generate("gemini-3.8-flash", attempts=2)
    except errors.APIError as error:
        if error.code != 503:
            raise

    print("⚠️ Gemini 3.8 Flash 暫時無法使用（HTTP 503），試一次 Gemini 3.5 Flash-Lite。")
    return generate("gemini-3.5-flash-lite", attempts=1)


def is_pdf_already_processed(pdf_path: str, output_dir: str) -> bool:
    """檢查該 PDF 是否已經在對應科目資料夾中生成過筆記"""
    if not os.path.exists(output_dir):
        return False
    existing_mds = glob.glob(f"{output_dir}/*.md")
    for md_file in existing_mds:
        try:
            with open(md_file, "r", encoding="utf-8") as f:
                head_text = f.read(500)
                if any(f"`{value}`" in head_text for value in {pdf_path, Path(pdf_path).as_posix()}):
                    return True
        except Exception:
            pass
    return False

def pdf_subject(pdf_path: str) -> str:
    path = Path(pdf_path)
    if len(path.parts) > 2:
        return path.parts[1]
    return path.stem.split("_", 1)[0] if "_" in path.stem else "一般科目"


def process_single_pdf(target_pdf: str):
    base_name = Path(target_pdf).stem
    subject = pdf_subject(target_pdf)

    date_match = re.search(r'20\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])', target_pdf)
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

    print(f"🚀 開始處理檔案: {target_pdf} (科目: {subject}, 日期: {date_str})")

    img_prefix = f"{date_str}_{sanitize_filename(base_name)}"
    page_images, full_pages, api_image_parts = render_pdf_pages(target_pdf, asset_dir, img_prefix)

    # 關鍵升級：提高 max_output_tokens 並調低 temperature 確保推導嚴謹完整
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        max_output_tokens=16384,
        temperature=0.2,
    )

    prompt_text = (
        f"以上是「{subject}」科目於 {date_str} 的板書/講義（共 {len(page_images)} 頁）。\n"
        "請仔細辨識圖片中的所有手寫公式、下標符號與波形圖，並補全所有省略的代數推導步驟、"
        "物理意義解說與比較表格，撰寫成一份詳盡的教科書級複習筆記。\n"
        "請嚴格依照 ===TOPIC===、===SUMMARY===、===NOTES===、===DIAGRAMS===、===QUESTION=== 五個區塊標籤輸出。"
    )

    response = call_gemini_with_retry(
        contents=api_image_parts + [prompt_text],
        config=config
    )

    data = parse_tagged_response(response.text)
    topic = data["topic"]
    summary = data["summary"]
    notes = data["notes"]
    diagrams = data["diagrams"]
    question = data["question"]

    safe_topic = sanitize_filename(topic)
    output_md_path = f"{output_dir}/{date_str}_{safe_topic}.md"

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
        question = fix_github_latex(question)

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
        f"> **科目**：`{subject}` ｜ **上課日期**：`{date_str}` ｜ **原始檔案**：`{Path(target_pdf).as_posix()}`\n\n"
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

    if question:
        try:
            auth = Auth.Token(GITHUB_TOKEN)
            gh = Github(auth=auth)
            repo = gh.get_repo(REPO_NAME)
            issue_title = f"【{subject}｜筆記待補全】{date_str} {topic}"
            issue_body = (
                "### AI 學習助理提問\n"
                f"在閱讀 `{Path(target_pdf).as_posix()}` 時發現部分內容需釐清：\n\n"
                f"> **{question}**\n\n"
                "---\n"
                f"💡 **回覆說明**：直接在此 Issue 留言回答，補全資訊將會在後續整合至 `{output_md_path}`。\n"
            )
            repo.create_issue(title=issue_title, body=issue_body)
            print(f"✅ 已針對缺漏建立 Issue 發問: {issue_title}")
        except Exception as e:
            print(f"⚠️ 建立 Issue 時發生警告（不影響筆記儲存）: {e}")

def process_all_unprocessed_pdfs():
    paths = [
        path
        for root in ("原始檔案", "incoming")
        for path in sorted(glob.glob(f"{root}/**/*.pdf", recursive=True))
        if is_valid_pdf(path)
    ]
    if not paths:
        print("ℹ️ 未找到任何有效的 PDF 檔案。")
        return

    groups = {}
    for pdf_path in paths:
        subject = pdf_subject(pdf_path)
        with open(pdf_path, "rb") as pdf_file:
            digest = hashlib.file_digest(pdf_file, "sha256").digest()
        groups.setdefault((subject, digest), []).append(pdf_path)

    processed_count = 0
    for (subject, _), copies in groups.items():
        output_dir = f"notes/{subject}"
        if any(is_pdf_already_processed(path, output_dir) for path in copies):
            print(f"⏭️ 已存在對應筆記，自動略過: {copies[0]}")
            continue

        process_single_pdf(copies[0])
        processed_count += 1

    if processed_count == 0:
        print("ℹ️ 沒有待處理的 PDF，已保留現有筆記。")


def main():
    try:
        process_all_unprocessed_pdfs()
    except errors.APIError as error:
        if error.code == 429:
            print("❌ Gemini 暫時超過請求限制或額度。未完成的 PDF 留待下次執行。")
        elif error.code in (408, 500, 502, 503, 504):
            print(f"❌ Gemini 暫時無法完成請求（HTTP {error.code}）。未完成的 PDF 留待下次執行。")
        else:
            raise
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
