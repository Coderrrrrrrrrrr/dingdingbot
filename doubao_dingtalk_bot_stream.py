import os
import json
import asyncio
import requests
import psutil
import platform
import traceback
import time
from dotenv import load_dotenv
from openai import OpenAI
from dingtalk_stream import DingTalkStreamClient, ChatbotHandler, Credential, ChatbotMessage, AckMessage
from doc_gen import InvestmentDocGenerator
from docx import Document

# Load environment variables from .env file
load_dotenv()

# 1. Configuration - Using Environment Variables
DOUBAO_API_KEY = os.getenv("DOUBAO_API_KEY")
DOUBAO_BASE_URL = os.getenv("DOUBAO_BASE_URL")
DOUBAO_MODEL = os.getenv("DOUBAO_MODEL")
# User's DingTalk App (Self-built App)
CLIENT_ID = os.getenv("DINGTALK_CLIENT_ID")
CLIENT_SECRET = os.getenv("DINGTALK_CLIENT_SECRET")
# Path to dws CLI
DWS_PATH = os.getenv("DWS_PATH")

# OA System Credentials (Optional, for automation)
OA_USERNAME = os.getenv("OA_USERNAME")
OA_PASSWORD = os.getenv("OA_PASSWORD")
OA_URL = os.getenv("OA_URL")

# Validate required environment variables
if not DOUBAO_API_KEY:
    raise ValueError("Missing required environment variable: DOUBAO_API_KEY")
if not CLIENT_ID:
    raise ValueError("Missing required environment variable: DINGTALK_CLIENT_ID")
if not CLIENT_SECRET:
    raise ValueError("Missing required environment variable: DINGTALK_CLIENT_SECRET")

print(f"✅ Configuration loaded from environment variables")
print(f"   - Model: {DOUBAO_MODEL}")
print(f"   - Client ID: {CLIENT_ID[:10]}...")

client = OpenAI(api_key=DOUBAO_API_KEY, base_url=DOUBAO_BASE_URL)
doc_generator = InvestmentDocGenerator(output_dir=os.getenv("OUTPUT_DIR"))

# 2. Session & Deduplication Manager
class SessionManager:
    def __init__(self, max_history=10):
        self.sessions = {}
        self.max_history = max_history
        self.processed_msgs = set() # 记录已处理的消息 ID

    def is_duplicate(self, msg_id):
        if not msg_id: return False
        if msg_id in self.processed_msgs: return True
        # 保持集合大小，防止内存溢出
        if len(self.processed_msgs) > 1000: self.processed_msgs.clear()
        self.processed_msgs.add(msg_id)
        return False

    def add_message(self, session_id, role, content):
        if session_id not in self.sessions:
            self.sessions[session_id] = []
        self.sessions[session_id].append({"role": role, "content": content})
        if len(self.sessions[session_id]) > self.max_history:
            self.sessions[session_id] = self.sessions[session_id][-self.max_history:]

    def get_history(self, session_id):
        return self.sessions.get(session_id, [])

# 创建全局会话管理器实例
session_manager = SessionManager()

# 3. Custom Skills
def get_system_status():
    """Returns the current system CPU and Memory usage."""
    cpu_usage = psutil.cpu_percent(interval=1)
    memory_info = psutil.virtual_memory()
    return {
        "status": "Healthy",
        "os": platform.system(),
        "cpu_usage": f"{cpu_usage}%",
        "memory_usage": f"{memory_info.percent}%",
        "available_memory": f"{memory_info.available // (1024 * 1024)} MB"
    }

def web_search(query):
    """Placeholder for web search."""
    print(f"Executing Web Search: {query}")
    return {"result": f"Found results for {query}: [Placeholder result]"}

# 4. OA Automation Tool (持久化浏览器版)
oa_context = {"browser": None, "playwright": None, "page": None}
oa_lock = asyncio.Lock() # 增加锁，防止多个请求同时竞争浏览器

async def operate_oa_system(action_desc, content_summary=None):
    global oa_context
    async with oa_lock: # 确保同一时间只有一个 OA 自动化流程在执行
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return {"error": "Playwright is not installed."}
        
        print(f"🚀 OA Automated Flow Started: {action_desc}")
        
        try:
            # 初始化或复用浏览器逻辑增强
            if not oa_context["playwright"]:
                oa_context["playwright"] = await async_playwright().start()
            
            # 检查浏览器是否存活，如果不存活则重置
            if oa_context["browser"]:
                try:
                    # 尝试一个轻量级操作检查存活
                    await oa_context["browser"].version()
                except:
                    print("Notice: Browser disconnected, re-initializing...")
                    oa_context["browser"] = None
                    oa_context["page"] = None

            if not oa_context["browser"]:
                oa_context["browser"] = await oa_context["playwright"].chromium.launch(
                    headless=False, 
                    args=["--disable-blink-features=AutomationControlled"]
                )
                # 创建新页面并存储
                context = await oa_context["browser"].new_context(viewport={'width': 1920, 'height': 1080})
                oa_context["page"] = await context.new_page()

            page = oa_context["page"]
            if not page:
                 # 二次保险
                 context = await oa_context["browser"].new_context(viewport={'width': 1920, 'height': 1080})
                 oa_context["page"] = await context.new_page()
                 page = oa_context["page"]

            # 检查是否需要登录 (如果当前不在系统内)
            current_url = page.url
            if "oa.cnyig.com" not in current_url or "login" in current_url:
                print("Step 1: Filling Credentials...")
                await page.goto(OA_URL, wait_until="domcontentloaded")
                await page.fill("#loginid", OA_USERNAME)
                await page.fill("#userpassword", OA_PASSWORD)
                
                try:
                    if not await page.query_selector("#verifyCode"):
                        await page.click("#login", timeout=5000)
                except: pass

                # 等待登录成功
                print("Waiting for login success...")
                login_success = False
                for _ in range(120):
                    if await page.query_selector("[title='办文']") or await page.query_selector("text='首页'"):
                        login_success = True; break
                    await asyncio.sleep(1)
                if not login_success: raise Exception("登录确认超时")

            # 跳转到目标流程
            print("✅ System Ready. Routing to target form...")
            target_url = "https://oa.cnyig.com/spa/workflow/static4form/index.html#/main/workflow/req?iscreate=1&workflowid=380"
            await page.goto(target_url, wait_until="networkidle", timeout=60000)

            # 填报摘要
            if content_summary:
                print(f"Step 3: Filling summary...")
                for _ in range(10):
                    await asyncio.sleep(2)
                    selectors = ["textarea", "div.ck-content", "textarea[name^='field']", ".ant-input"]
                    filled = False
                    for sel in selectors:
                        try:
                            element = await page.wait_for_selector(sel, timeout=1000, state="visible")
                            if element:
                                # 彻底解除手动截断，相信大模型的智能浓缩能力
                                await page.fill(sel, content_summary)
                                print(f"🎯 Filled via {sel}, chars: {len(content_summary)}")
                                filled = True
                                break
                        except: continue
                    if filled: break

            # 确认标题后立即返回结果，不关闭浏览器
            try:
                await page.wait_for_selector("text='云南云投股权投资基金管理有限公司文件呈批单'", timeout=20000)
                msg = "🎯 已成功到达《文件呈批单》页面。页面已为您保持开启，请直接操作。"
            except:
                msg = "⚠️ 已到达流程页面，但未检测到特定标题。页面已为您保持开启，请检查。"

            print("Task done. Returning control to user.")
            return {"status": "success", "message": msg}

        except Exception as e:
            print(f"🚨 OA Execution Error: {str(e)}")
            return {"status": "error", "message": f"OA路由异常: {str(e)}"}

# 5. Investment Proposal Generator
async def generate_investment_proposal(topic):
    system_prompt = """你是一个投研专家，撰写格式标准的投资建议书。返回 JSON 列表。"""
    try:
        completion = client.chat.completions.create(
            model=DOUBAO_MODEL,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": f"撰写主题：{topic}"}],
            response_format={"type": "json_object"}
        )
        content = json.loads(completion.choices[0].message.content)
        sections = content.get("sections", content if isinstance(content, list) else [])
        if not sections and isinstance(content, dict):
            for v in content.values():
                if isinstance(v, list): sections = v; break
        filepath = doc_generator.generate(topic, sections)
        return {"status": "success", "file_path": filepath, "message": f"成功生成《{topic}》。"}
    except Exception as e:
        return {"error": str(e)}

# 6. Tools Definition
tools = [
    {
        "type": "function",
        "function": {
            "name": "dingtalk_command",
            "description": "Execute a DingTalk dws CLI command.",
            "parameters": {
                "type": "object",
                "properties": {
                    "args": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["args"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_status",
            "description": "获取服务器状态。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "搜索实时信息。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "generate_investment_proposal",
            "description": "生成投资建议书 Word。",
            "parameters": {
                "type": "object",
                "properties": {"topic": {"type": "string"}},
                "required": ["topic"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "operate_oa_system",
            "description": "自动化 OA 呈批流程。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action_desc": {"type": "string"},
                    "content_summary": {"type": "string", "description": "专业呈批件全文，必须遵循 OASummaryWriter 格式。"}
                },
                "required": ["action_desc"]
            }
        }
    }
]

# 7. Doubao Response Logic
async def get_doubao_response(user_input, history=[]):
    system_msg = """你是一个专业的公文撰写专家。请根据 Word 文档内容构造【OASummaryWriter】呈批摘要。

要求：
1. 严格遵循四段式：背景、事实论据（一是二是）、合规审查、决策建议。
2. 字数限制：全文必须精简在 500 字以内。
3. 逻辑完整：严禁在句子中间截断，必须包含“妥否，请领导批示。”作为结尾。
4. 附件引用：必须保留原文档中的附件编号（如：附件4）。

--- 示例结构 ---
近日，公司[主体]拟召开会议，审议：[议案全称]。
根据材料，一是，[关键事实A]（附件X）；二是，[关键事实B]（附件Y）。
根据[制度名称]，本次事项符合决策流程。
建议：拟同意签署决议。
妥否，请领导批示。
--- 示例结束 ---"""
    
    messages = [{"role": "system", "content": system_msg}] + history + [{"role": "user", "content": user_input}]
    try:
        # 增加 max_tokens 确保公文全文不被截断
        completion = client.chat.completions.create(
            model=DOUBAO_MODEL, 
            messages=messages, 
            tools=tools, 
            tool_choice="auto",
            max_tokens=2048
        )
        msg = completion.choices[0].message
        if msg.tool_calls:
            messages.append(msg)
            for tool in msg.tool_calls:
                name, args = tool.function.name, json.loads(tool.function.arguments)
                if name == "dingtalk_command": output = await run_dws_command(args['args'])
                elif name == "get_system_status": output = get_system_status()
                elif name == "web_search": output = web_search(args['query'])
                elif name == "generate_investment_proposal": output = await generate_investment_proposal(args['topic'])
                elif name == "operate_oa_system": output = await operate_oa_system(args['action_desc'], args.get('content_summary'))
                else: output = {"error": "Unknown"}
                messages.append({"tool_call_id": tool.id, "role": "tool", "name": name, "content": json.dumps(output)})
            
            final_res = client.chat.completions.create(model=DOUBAO_MODEL, messages=messages, max_tokens=2048)
            return final_res.choices[0].message.content
        return msg.content
    except Exception as e:
        return f"错误: {str(e)}"

# 8. DingTalk File helper
class DingTalkFileHelper:
    def __init__(self, cid, secret):
        self.cid, self.secret, self.token, self.expiry = cid, secret, None, 0
    def get_token(self):
        import time
        if self.token and time.time() < self.expiry: return self.token
        try:
            res = requests.post("https://api.dingtalk.com/v1.0/oauth2/accessToken", json={"appKey": self.cid, "appSecret": self.secret}, timeout=10)
            if res.status_code == 200:
                d = res.json()
                self.token, self.expiry = d.get('accessToken'), time.time() + 7100
                print("DEBUG: Token 刷新成功")
                return self.token
            else:
                print(f"DEBUG: Token 刷新失败 (HTTP {res.status_code}): {res.text}")
        except Exception as e:
            print(f"DEBUG: Token 获取异常: {e}")
        return None
    def download_file(self, code, save_path):
        """按照钉钉最新文档，调用 download 接口。可能直接返回流，也可能返回 JSON 包含 downloadUrl"""
        t = self.get_token()
        if not t: return False
        
        url = "https://api.dingtalk.com/v1.0/robot/messageFiles/download"
        headers = {"x-acs-dingtalk-access-token": t, "Content-Type": "application/json"}
        payload = {"downloadCode": code, "robotCode": self.cid}
        
        try:
            print(f"DEBUG: 正在请求钉钉下载接口, code: {code[:10]}...")
            res = requests.post(url, headers=headers, json=payload, timeout=30)
            
            if res.status_code == 200:
                # 判断是否是 JSON (有时返回 JSON 包含下载链接，有时直接返回流)
                content_type = res.headers.get("Content-Type", "").lower()
                
                if "application/json" in content_type:
                    d = res.json()
                    real_url = d.get("downloadUrl")
                    if real_url:
                        print(f"DEBUG: 获取到二次下载链接，正在从 OSS 拉取...")
                        file_res = requests.get(real_url, timeout=60)
                        if file_res.status_code == 200:
                            with open(save_path, 'wb') as f: f.write(file_res.content)
                            print(f"✅ 文件从 OSS 下载成功: {save_path}")
                            return True
                    else:
                        print(f"❌ JSON 中未找到 downloadUrl: {d}")
                        return False
                else:
                    # 直接是文件流
                    with open(save_path, 'wb') as f: f.write(res.content)
                    print(f"✅ 文件流下载成功: {save_path}")
                    return True
            else:
                print(f"❌ 接口请求失败 (HTTP {res.status_code}): {res.text}")
                return False
        except Exception as e:
            print(f"🚨 下载处理异常: {e}")
            return False

file_helper = DingTalkFileHelper(CLIENT_ID, CLIENT_SECRET)

# 9. Handler
class DoubaoChatBotHandler(ChatbotHandler):
    async def process(self, callback):
        try:
            raw = callback.data
            # 消息去重，防止钉钉重试导致的重复执行
            msg_id = raw.get('msgId')
            if session_manager.is_duplicate(msg_id):
                print(f"DEBUG: 消息 {msg_id} 是重复的重试请求，拦截处理。")
                return AckMessage.STATUS_OK, 'OK'
                
            msg = ChatbotMessage.from_dict(raw)
            # Use getattr for robustness or fallback to raw data
            sid = getattr(msg, 'conversation_id', None) or raw.get('conversationId', 'default_session')
            mtype = (getattr(msg, 'msg_type', None) or raw.get('msgtype') or raw.get('msgType') or "text").lower()
            
            print(f"DEBUG: Received message. Topic: unknown")
            
            content = ""
            if mtype == "text":
                if msg.text and hasattr(msg.text, 'content'):
                    content = msg.text.content.strip()
                if not content and 'text' in raw and isinstance(raw['text'], dict):
                    content = raw['text'].get('content', '').strip()
                if not content and 'text' in raw and isinstance(raw['text'], str):
                    content = raw['text'].strip()
                
                # 优先处理：驾驶舱风格选择指令 (1 或 2)
                if content in ["1", "2"]:
                    history = session_manager.get_history(sid)
                    data_item = next((h['content'] for h in reversed(history) if "[FINANCIAL_DATA]" in h['content']), None)
                    if data_item:
                        try:
                            from financial_engine import financial_engine
                            data = json.loads(data_item.replace("[FINANCIAL_DATA]", ""))
                            style_map = {"1": "business", "2": "rough"}
                            style_id = style_map[content]
                            self.reply_text(f"🎨 正在为您生成【{style_id}】风格对标驾驶舱...", msg)
                            html_path = financial_engine.render_dashboard(data, style_id)
                            self.reply_text(f"✨ 对标驾驶舱生成成功！\n📂 路径：{html_path}", msg)
                            return AckMessage.STATUS_OK, 'OK'
                        except Exception as e:
                            self.reply_text(f"渲染失败: {e}", msg)
                            return AckMessage.STATUS_OK, 'OK'

                # 优先处理：指标选择指令 (如 "1,2,5" 或 "1 2 5")
                history = session_manager.get_history(sid)
                if any("[PENDING_INDICATOR_CHOICE]" in h['content'] for h in history):
                    import re
                    choices = re.findall(r"\d+", content)
                    if choices:
                        self.reply_text(f"🔢 正在按照您选定的 {len(choices)} 项指标深度抓取 10 家对标公司数据...", msg)
                        choice_item = next((h['content'] for h in reversed(history) if "[PENDING_INDICATOR_CHOICE]" in h['content']), None)
                        meta_data = json.loads(choice_item.replace("[PENDING_INDICATOR_CHOICE]", ""))
                        all_indicators = meta_data["indicators"]
                        selected_inds = [all_indicators[int(c)-1] for c in choices if 0 < int(c) <= len(all_indicators)]
                        from competitor_analysis import get_financial_data_final
                        peers = await get_financial_data_final(meta_data["decision"], selected_inds, meta_data["target_date"])
                        if not peers:
                            self.reply_text("🚨 抱歉，未能抓取到对标数据，请检查网络或更换关键词重试。", msg)
                            return AckMessage.STATUS_OK, 'OK'
                        # 组合最终数据
                        excel_data = meta_data["excel_data"]
                        # 核心修正：根据用户选择的指标，同步更新 Excel 中的数据维度
                        user_all_indicators = excel_data.get("core_metrics", {}).get("all_indicators", {})
                        
                        mapping_keys = ["资产总额", "主营业务收入", "净利润", "经营性净现金流量"]
                        for i, ind_name in enumerate(selected_inds[:4]):
                            clean_name = re.sub(r'\s+', '', ind_name)
                            val = user_all_indicators.get(clean_name, 0.0)
                            if val == 0:
                                for k, v in user_all_indicators.items():
                                    if ind_name in k or k in ind_name:
                                        val = v; break
                            excel_data["core_metrics"][mapping_keys[i]] = val
                            excel_data["core_metrics"][f"{mapping_keys[i]}_label"] = ind_name

                        excel_data["metadata"]["corp_name"] = meta_data["decision"]["user_corp_name"]
                        excel_data["competitors"] = peers
                        session_manager.add_message(sid, "assistant", f"[FINANCIAL_DATA]{json.dumps(excel_data)}")
                        self.reply_text(f"✅ 数据抓取成功！\n共获取 {len(peers)} 家公司、{len(selected_inds)} 项自定义指标。\n请选择可视化风格：\n1. 云投商务风\n2. 宝爷手绘风", msg)
                        return AckMessage.STATUS_OK, 'OK'

                # 优先处理：对标公司名称输入
                if any("[PENDING_CORP_INFO]" in h['content'] for h in history):
                    # 此时 content 是用户输入的完整需求，如“云南神火，铝冶炼，2025年三季度”
                    self.reply_text(f"🔍 收到需求！正在为您从金融终端调取满足【{content}】要求的所有可用指标清单...", msg)
                    data_item = next((h['content'] for h in reversed(history) if "[PENDING_CORP_INFO]" in h['content']), None)
                    excel_data = json.loads(data_item.replace("[PENDING_CORP_INFO]", ""))
                    from competitor_analysis import get_financial_meta
                    meta = await get_financial_meta(content, client, DOUBAO_MODEL)
                    if not meta:
                        self.reply_text("🚨 抱歉，解析行业及期次失败，请检查年份或行业关键词（如：2025年一季报）。", msg)
                        return AckMessage.STATUS_OK, 'OK'
                    meta["excel_data"] = excel_data
                    session_manager.add_message(sid, "assistant", f"[PENDING_INDICATOR_CHOICE]{json.dumps(meta)}")
                    # 构造指标列表发送给用户
                    indicators = meta["indicators"]
                    ind_list_str = "\n".join([f"{i+1}. {ind}" for i, ind in enumerate(indicators)])
                    
                    if len(indicators) > 50:
                        txt_path = f"指标清单_{meta['target_date']}.txt"
                        with open(txt_path, 'w', encoding='utf-8') as f:
                            f.write(ind_list_str)
                        self.reply_text(f"📊 已为您锁定 10 家行业巨头。由于可选指标高达 {len(indicators)} 项，钉钉文本无法完全显示。我已为您在本地生成了指标清单文件，您可以查看该文件后回复编号（如 1,2,5）。\n\n📂 清单保存于：{os.path.abspath(txt_path)}", msg)
                    else:
                        self.reply_text(f"📊 已为您锁定 10 家行业巨头。请回复编号选择您感兴趣的财务指标（多选请用逗号隔开，如 1,2,5）：\n\n{ind_list_str}", msg)
                    return AckMessage.STATUS_OK, 'OK'

            elif mtype == "file":
                c = raw.get('content', '{}')
                finfo = json.loads(c) if isinstance(c, str) else c
                fname, fcode = finfo.get('fileName', ''), finfo.get('downloadCode')
                
                # Excel 路径
                if fname.endswith('.xlsx'):
                    self.reply_text(f"📊 正在解析财务报表: {fname}...", msg)
                    path = f"fin_{int(time.time())}.xlsx"
                    if file_helper.download_file(fcode, path):
                        try:
                            from financial_engine import financial_engine
                            data = financial_engine.process_excel(path)
                            # 设置待定状态
                            session_manager.add_message(sid, "assistant", f"[PENDING_CORP_INFO]{json.dumps(data)}")
                            self.reply_text(f"✅ 报表解析成功！\n请输入该【公司简称】及【主营业务】(例如: 云南神火, 铝冶炼)，我将为您搜索对标数据。", msg)
                            os.remove(path)
                        except Exception as e:
                            self.reply_text(f"解析错误: {e}", msg)
                    return AckMessage.STATUS_OK, 'OK'

                # Word 路径
                elif fname.endswith('.docx'):
                    self.reply_text(f"📝 正在解析文档: {fname}...", msg)
                    path = f"tmp_{int(time.time())}.docx"
                    if file_helper.download_file(fcode, path):
                        from docx import Document
                        doc = Document(path)
                        text = "\n".join([p.text for p in doc.paragraphs])
                        os.remove(path)
                        content = f"[已读取 Word 文档：{fname}]\n{text}"
                    else:
                        self.reply_text("文件下载失败", msg)
                        return AckMessage.STATUS_OK, 'OK'
            
            if content:
                print(f"DEBUG: Input: {content[:50]}")
                history = session_manager.get_history(sid)
                ans = await get_doubao_response(content, history)
                session_manager.add_message(sid, "user", content)
                session_manager.add_message(sid, "assistant", ans)
                self.reply_markdown("助手回复", ans, msg)
            else:
                print("DEBUG: Content is empty, no response sent.")
            return AckMessage.STATUS_OK, 'OK'
        except Exception as e:
            print(f"Error: {str(e)}"); traceback.print_exc()
            return AckMessage.STATUS_OK, 'OK'

def main():
    print("Bot starting with memory...")
    client = DingTalkStreamClient(Credential(CLIENT_ID, CLIENT_SECRET))
    client.register_callback_handler(ChatbotMessage.TOPIC, DoubaoChatBotHandler())
    client.start_forever()

if __name__ == "__main__":
    main()
