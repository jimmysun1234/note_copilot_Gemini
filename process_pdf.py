import os
import glob
import json
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

# 定義嚴格的輸出資料結構，杜絕 JSON 解析錯誤或 LaTeX 反斜線跳脫崩潰
class NoteOutput(BaseModel):
    summary: str = Field(description="3~5 個重點的核心精華摘要 (Markdown 格式)")
    notes: str = Field(description="完整的結構化筆記，包含章節、公式推導、關鍵觀念、範例 (Markdown 格式)")
    question: Optional[str] = Field(default=None, description="如果板書有缺漏、遮蔽、筆跡模糊或邏輯中斷，請提出『一個』最關鍵的具體問題向使用者確認。如果內容完整無缺漏，請填 null。")

SYSTEM_PROMPT = """
你是一位專業的高效學習助理與知識工程師。你的任務是閱讀使用者上傳的課堂板書/筆記 PDF，將其整理為條理分明、概念嚴謹的 Markdown 筆記。
若有數學或電路公式，請使用標準 LaTeX 語法（如 $...$ 或 $$...$$）呈現。
"""

def process_latest_pdf():
    pdf_files = glob.glob("incoming/*.pdf")
    if not pdf_files:
        print("未在 incoming/ 找到任何 PDF 檔案。")
        return

    latest_pdf = max(pdf_files, key=os.path.getctime)
    pdf_name = os.path.basename(latest_pdf)
    base_name = os.path.splitext(pdf_name)[0]
    output_md_path = f"notes/{base_name}.md"

    print(f"正在處理檔案: {latest_pdf}")

    with open(latest_pdf, "rb") as f:
        pdf_bytes = f.read()

    pdf_part = types.Part.from_bytes(
        data=pdf_bytes,
        mime_type="application/pdf"
    )

    # 調用最新的 gemini-3.8-flash 模型並強制結構化輸出
    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=[
            pdf_part,
            "請詳細閱讀這份板書 PDF，並依照規範產出摘要、完整結構化筆記與單題缺漏提問。"
        ],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=NoteOutput,
        )
    )

    # 安全解析回傳結果
    if response.parsed:
        result = response.parsed
        summary = result.summary
        notes = result.notes
        question = result.question
    else:
        data = json.loads(response.text)
        summary = data.get("summary", "")
        notes = data.get("notes", "")
        question = data.get("question")

    prompt_block = (
        "```markdown\n"
        f"你現在是該領域的教學專家。請根據上述「{base_name}」的筆記內容，出 3 題觀念驗證題（包含 1 題陷阱選擇題與 2 題推導/問答題），並附上詳細詳解與評分標準。\n"
        "```"
    )

    final_content = (
        f"# 課堂筆記：{base_name}\n\n"
        f"## 📌 3 分鐘核心摘要\n{summary}\n\n"
        f"---\n\n"
        f"## 📖 結構化重點筆記\n{notes}\n\n"
        f"---\n\n"
        f"## 💡 後續複習提示詞 (Prompt)\n{prompt_block}\n"
    )

    os.makedirs("notes", exist_ok=True)
    with open(output_md_path, "w", encoding="utf-8") as f:
        f.write(final_content)
    print(f"✅ 筆記已成功寫入 {output_md_path}")

    # 若有缺漏則建立 Issue 發問（加上 try-except 確保絕不影響筆記產出）
    if question and str(question).strip().lower() != "null":
        try:
            auth = Auth.Token(GITHUB_TOKEN)
            gh = Github(auth=auth)
            repo = gh.get_repo(REPO_NAME)
            issue_title = f"【筆記待補全】{pdf_name} 內容確認"
            issue_body = (
                "### AI 學習助理提問\n"
                f"在閱讀 `{pdf_name}` 時發現部分內容需釐清：\n\n"
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
