import streamlit as st
import os
import glob
import pandas as pd
from datetime import datetime, timedelta
from pypdf import PdfReader
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import time

# --- 1. 系統設定 ---
st.set_page_config(page_title="創傷知情 AI 家教 (研究版)", layout="wide")

# --- Google Sheets 背景自動上傳函式 (Auto-Save 版) ---
def auto_save_to_google_sheets(user_id, chat_history, lang):
    """每次對話更新時，自動在背景覆寫/更新該次對話紀錄"""
    if not chat_history:
        return False
        
    try:
        # 1. 檢查 Secrets 是否存在
        if "gcp_service_account" not in st.secrets:
            st.error("❌ 錯誤：找不到 Google Cloud 金鑰 (Secrets)。")
            return False

        # 2. 連線設定
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds_dict = dict(st.secrets["gcp_service_account"])
        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")

        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        
        # 3. 開啟試算表
        target_sheet_name = "2025創傷知情研習數據" 
        sheet = client.open(target_sheet_name)

        # 4. 取得或自動建立 'Tutor' 分頁
        try:
            worksheet = sheet.worksheet("Tutor")
        except gspread.WorksheetNotFound:
            worksheet = sheet.add_worksheet(title="Tutor", rows="1000", cols="10")
            worksheet.append_row(["登入時間", "登出時間", "學員編號", "使用分鐘數", "累積使用次數", "完整對話紀錄"])
        
        # 5. 時間計算
        tw_fix = timedelta(hours=8)
        start_t = st.session_state.get('start_time', datetime.now())
        login_str = (start_t + tw_fix).strftime("%Y-%m-%d %H:%M:%S")
        end_t = datetime.now()
        logout_str = (end_t + tw_fix).strftime("%Y-%m-%d %H:%M:%S") # 視為最後更新時間
        duration_mins = round((end_t - start_t).total_seconds() / 60, 2)
        
        # 6. 整理對話內容
        context_info = f"使用語言: {lang}"
        full_conversation = f"【設定參數】：{context_info}\n\n"
        for msg in chat_history:
            role = msg.get("role", "Unknown")
            content = ""
            if "parts" in msg:
                content = msg["parts"][0] if isinstance(msg["parts"], list) else str(msg["parts"])
            elif "content" in msg:
                content = msg["content"]
            full_conversation += f"[{role}]: {content}\n"

        # 7. 尋找並更新，或新增一筆
        records = worksheet.get_all_records()
        row_to_update = None
        col_logins = worksheet.col_values(1) # 第一欄：登入時間
        col_ids = worksheet.col_values(3)    # 第三欄：學員編號
        
        for i in range(1, len(col_logins)): # 跳過標題列
            if i < len(col_ids) and col_logins[i] == login_str and str(col_ids[i]) == str(user_id):
                row_to_update = i + 1 # Gspread 索引從 1 開始
                break
                
        # 計算累積次數
        login_count = col_ids.count(str(user_id))
        if row_to_update is None:
            login_count += 1 # 新增一筆
            
        data_row = [login_str, logout_str, user_id, duration_mins, login_count, full_conversation]
        
        if row_to_update:
            # 更新既有列 (A:F)
            cell_range = f'A{row_to_update}:F{row_to_update}'
            worksheet.update(cell_range, [data_row])
        else:
            # 新增一列
            worksheet.append_row(data_row)
            
        return True

    except Exception as e:
        print(f"背景上傳發生錯誤: {str(e)}") 
        return False

# --- API 輪替與防呆發送機制 (角色強化版) ---
def send_message_safely(text):
    """
    發送訊息，若失敗則自動切換至下一把 API Key 重試。
    加入 system_instruction 防護機制，確保切換 Key 時角色絕不混亂。
    """
    time.sleep(1) # [防呆] 強制減速 1 秒
    
    # 我們的設計中，history 的第一筆 [0] 永遠是系統設定 (sys_prompt)
    system_prompt = st.session_state.history[0]["content"]
    
    # 取得除了第一筆 (sys_prompt) 之外的純對話歷史
    gemini_history = []
    for msg in st.session_state.history[1:]:
        g_role = "model" if msg["role"] == "assistant" else "user"
        gemini_history.append({"role": g_role, "parts": [msg["content"]]})
        
    api_keys = st.session_state.api_keys_list
    total_keys = len(api_keys)
    
    # 開始輪替嘗試
    for i in range(total_keys):
        current_key_index = (st.session_state.current_key_index + i) % total_keys
        active_key = api_keys[current_key_index]
        
        try:
            # 使用當前的 Key 初始化連線
            genai.configure(api_key=active_key)
            
            # 將 System Prompt 綁定為 system_instruction
            model = genai.GenerativeModel(
                model_name=st.session_state.valid_model_name,
                system_instruction=system_prompt,
                safety_settings={
                    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
                    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
                    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
                    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
                }
            )
            
            # 使用純淨的歷史紀錄建立 session
            chat_session = model.start_chat(history=gemini_history)
            response = chat_session.send_message(text)
            
            # 成功則紀錄使用的 key index 並回傳
            st.session_state.current_key_index = current_key_index
            return response.text
            
        except Exception as e:
            error_msg = str(e).lower()
            st.toast(f"⚠️ Key {current_key_index + 1} 發生狀況，嘗試切換...", icon="🔄")
            
            if i == total_keys - 1:
                if "429" in error_msg or "quota" in error_msg:
                    st.warning("🐌 哎呀！您輸入的速度太快，或是目前所有 API 額度都耗盡了。請稍等 1 分鐘後再試喔！")
                    return None
                else:
                    raise e

# --- 格式化下載內容函式 ---
def convert_history_to_txt(history):
    text_content = ""
    for msg in history:
        # 隱藏系統的設定指令，讓下載的紀錄更乾淨
        if "Role: You are a \"Trauma-Informed Care Tutor\"" not in msg["content"]:
            role_name = "AI 家教" if msg["role"] == "assistant" else "學員"
            content = msg["content"]
            text_content += f"【{role_name}】：\n{content}\n\n{'='*20}\n\n"
    return text_content

# 初始化 Session State
if "history" not in st.session_state: st.session_state.history = []
if "loaded_text" not in st.session_state: st.session_state.loaded_text = ""
if "user_nickname" not in st.session_state: st.session_state.user_nickname = ""
if "start_time" not in st.session_state: st.session_state.start_time = datetime.now()
if "chat_session_initialized" not in st.session_state: st.session_state.chat_session_initialized = False

# 多重 API Key 記憶機制
if "raw_api_key_input" not in st.session_state: st.session_state.raw_api_key_input = ""
if "api_keys_list" not in st.session_state: st.session_state.api_keys_list = []
if "current_key_index" not in st.session_state: st.session_state.current_key_index = 0
if "valid_model_name" not in st.session_state: st.session_state.valid_model_name = "gemini-2.5-flash" # 預設改為 2.5 flash

# --- 2. 登入區 (編號制) ---
if not st.session_state.user_nickname:
    st.title("📚 創傷知情 AI 家教 (Tutor)")
    st.info("請輸入您的研究編號 (ID) 以開始學習。")
    
    nickname_input = st.text_input("請輸入您的編號：", placeholder="例如：001, 002...") 
    
    if st.button("🚀 進入教室"):
        if nickname_input.strip():
            st.session_state.user_nickname = nickname_input
            st.session_state.start_time = datetime.now()
            st.rerun()
        else:
            st.error("❌ 編號不能為空！")
    st.stop()

# --- 3. 側邊欄設定 ---
st.sidebar.title(f"👤 學員: {st.session_state.user_nickname}")
st.sidebar.markdown("*(系統已開啟自動存檔功能)*")
st.sidebar.markdown("---")

# 返回首頁按鈕
if st.session_state.chat_session_initialized:
    st.sidebar.markdown("### 🏠 導覽")
    if st.sidebar.button("返回首頁 / 重新開始", type="secondary"):
        st.session_state.history = []
        st.session_state.chat_session_initialized = False
        st.session_state.start_time = datetime.now() 
        st.rerun()

# 下載對話紀錄
st.sidebar.markdown("---")
if st.session_state.history:
    st.sidebar.subheader("💾 個人備份")
    chat_txt = convert_history_to_txt(st.session_state.history)
    st.sidebar.download_button(
        label="📥 下載對話紀錄 (.txt)",
        data=chat_txt,
        file_name=f"Tutor_History_{st.session_state.user_nickname}.txt",
        mime="text/plain",
        help="點擊下載這份對話紀錄到您的電腦中保存"
    )

st.sidebar.markdown("---")
st.sidebar.warning("🔑 請輸入您的 Gemini API Key (可輸入多組)")
st.sidebar.markdown("<small>提示：輸入多組 Key 請用半形逗號 `,` 隔開，可防當機</small>", unsafe_allow_html=True)

# 利用 value 綁定 session_state，讓系統記住 API Key
input_key = st.sidebar.text_input("在此貼上您的 API Key", type="password", value=st.session_state.raw_api_key_input)

if input_key:
    st.session_state.raw_api_key_input = input_key
    st.session_state.api_keys_list = [k.strip() for k in input_key.split(",") if k.strip()]

if not st.session_state.api_keys_list:
    st.info("💡 提示：請先在側邊欄輸入至少一組 API Key，否則系統無法運作。")
    st.stop() 

# 模型偵測
if st.session_state.api_keys_list:
    try:
        genai.configure(api_key=st.session_state.api_keys_list[0])
        available_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        if available_models:
            st.session_state.valid_model_name = st.sidebar.selectbox("🤖 AI 模型", available_models, index=available_models.index("models/gemini-2.5-flash") if "models/gemini-2.5-flash" in available_models else 0)
    except: 
        st.sidebar.error("❌ 第一把 API Key 無效，請檢查。")

# 選項設定
lang = st.sidebar.selectbox("🌐 選擇對話語言", ["繁體中文", "粵語", "English"])
st.session_state.current_lang = lang

st.sidebar.caption(f"🛡️ 目前備妥 {len(st.session_state.api_keys_list)} 把 API Key 輪替中")

# --- 4. 自動讀取教材 ---
if not st.session_state.loaded_text:
    combined_text = ""
    pdf_files = glob.glob("*.pdf") + glob.glob("*.PDF")
    if pdf_files:
        with st.spinner(f"📚 正在內化 {len(pdf_files)} 份教材..."):
            try:
                for filename in pdf_files:
                    reader = PdfReader(filename)
                    for page in reader.pages:
                        text = page.extract_text()
                        if text: combined_text += text + "\n"
                st.session_state.loaded_text = combined_text
                st.toast(f"✅ 已載入 {len(pdf_files)} 份教材")
            except Exception as e:
                st.error(f"教材讀取失敗: {e}")
    else:
        st.warning("⚠️ 倉庫中找不到 PDF 檔案。")

# --- 5. 家教對話邏輯 (Mollick Tutor Prompt) ---
st.title("📖 創傷知情概念導讀區")

if st.session_state.loaded_text and st.session_state.api_keys_list and st.session_state.valid_model_name:

    if not st.session_state.chat_session_initialized:
        # 核心 Prompt：加入針對真實個案提問的拒絕機制
        sys_prompt = f"""
        Role: You are a "Trauma-Informed Care Tutor" (Mollick's Tutor Persona).
        Target Audience: A teacher learning about Trauma-Informed Care (TIC).
        Language: {lang}.
        
        Knowledge Base (Context): {st.session_state.loaded_text[:30000]}
        
        ### METHODOLOGY (Mollick's Tutor Model):
        1. **Assess & Explain:** When the user asks a question, explain the concept clearly and directly based on the Knowledge Base.
        2. **Provide Examples:** Always give a concrete, classroom-based example to illustrate the concept.
        3. **Check for Understanding (CRITICAL):** After explaining, *ALWAYS* ask the user a question to verify they understood.
            - Example Check: "Does this make sense to you?"
            - Example Check: "How might you see this appearing in your classroom?"
            - Example Check: "Could you try explaining the 'Flight' response back to me in your own words?"
        
        ### STRICT BOUNDARIES & RULES:
        1. **Scope Restriction:** You are an AI Tutor for *learning concepts*, NOT a supervisor for clinical cases.
        2. **Refusal Logic:** If the user asks for advice on specific, real-world student cases, personal counseling issues, or practical intervention strategies for specific students (e.g., "I have a student who does X, what should I do?"), you MUST politely decline.
        3. **Refusal Script:** "我是協助您學習創傷知情概念的 AI 家教，無法針對真實個案提供諮商建議或處遇策略。請我們回到教材內容，探討相關的理論概念好嗎？" (Translate this sentiment to the user's language if needed).
        4. **Redirect:** After declining, explicitly ask them to pose a question about a concept from the reading material instead.
        5. **Teaching Mode:** Do NOT just be a passive search engine. Be an *active teacher*.
        6. **Correction:** If the user's answer is wrong, correct them gently and re-explain.
        
        Start the conversation by introducing yourself as their TIC Tutor and asking what concept they would like to learn about today (e.g., 4F responses, window of tolerance, etc.).
        """
        
        welcome_msg = f"你好 {st.session_state.user_nickname} 老師！我是您的創傷知情 AI 家教。\n\n我的工作是協助您弄懂那些複雜的理論，並確認您能運用在教學上。今天您想了解哪個概念？（例如：創傷定義為何？什麼是TIC的核心原則？）"
        
        st.session_state.history = [{"role": "user", "content": sys_prompt}, {"role": "assistant", "content": welcome_msg}]
        st.session_state.chat_session_initialized = True
        auto_save_to_google_sheets(st.session_state.user_nickname, st.session_state.history, st.session_state.current_lang)

    for msg in st.session_state.history:
        role = "assistant" if msg["role"] == "assistant" else "user"
        # 隱藏系統 Prompt，不讓使用者看到落落長的設定
        if "Role: You are a \"Trauma-Informed Care Tutor\"" not in msg["content"]:
            with st.chat_message(role):
                st.write(msg["content"])

    if user_in := st.chat_input("詢問概念..."):
        st.session_state.history.append({"role": "user", "content": user_in})
        with st.chat_message("user"):
            st.write(user_in)
            
        with st.spinner("👩‍🏫 家教思考中 (為防超速，請稍候)..."):
            try:
                # 使用自動輪替機制的安全發送函式
                resp_text = send_message_safely(user_in)
                
                if resp_text: 
                    st.session_state.history.append({"role": "assistant", "content": resp_text})
                    # 背景自動存檔
                    auto_save_to_google_sheets(
                        st.session_state.user_nickname, 
                        st.session_state.history, 
                        st.session_state.current_lang
                    )
                    st.rerun()
            except Exception as e:
                st.error(f"❌ 發生嚴重錯誤: {e}")
