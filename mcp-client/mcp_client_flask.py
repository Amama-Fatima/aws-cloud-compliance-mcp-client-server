"""
Flask-based UI for Cloud Compliance Assistant - MODERN UI
Install: pip install flask flask-cors
Run: python mcp_client_flask_fixed.py
"""

import asyncio
import json
import subprocess
from typing import Optional, Dict, Any, List
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import ollama
from dataclasses import dataclass
from flask import Flask, render_template_string, request, jsonify
from flask_cors import CORS
import threading
import logging
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

@dataclass
class Message:
    role: str
    content: str


class CloudComplianceClient:
    def __init__(self, model_name: str = "llama3.2:3b"):
        self.model_name = model_name
        self.conversation_history: List[Message] = []
        self.available_tools: List[Dict] = []
        self.session = None
        self.loop = None
        
    def format_tools_for_llm(self, tools) -> str:
        """Format MCP tools into a prompt for the LLM"""
        tools_description = "Available tools:\n\n"
        
        for tool in tools:
            tools_description += f"- **{tool.name}**: {tool.description}\n"
            if hasattr(tool, 'inputSchema') and tool.inputSchema:
                schema = tool.inputSchema
                if 'properties' in schema:
                    tools_description += "  Parameters:\n"
                    for param_name, param_info in schema['properties'].items():
                        param_type = param_info.get('type', 'string')
                        param_desc = param_info.get('description', '')
                        required = param_name in schema.get('required', [])
                        req_marker = " (required)" if required else " (optional)"
                        tools_description += f"    - {param_name} ({param_type}){req_marker}: {param_desc}\n"
            tools_description += "\n"
        
        return tools_description
    
    async def call_mcp_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Call an MCP tool and return the result"""
        try:
            logger.info(f"📞 Calling MCP tool: {tool_name} with args: {arguments}")
            result = await self.session.call_tool(tool_name, arguments)
            logger.info(f"✅ Tool {tool_name} completed successfully")
            
            if hasattr(result, 'content') and result.content:
                if isinstance(result.content, list) and len(result.content) > 0:
                    response = result.content[0].text
                    logger.info(f"📦 Tool response length: {len(response)} chars")
                    return response
                return str(result.content)
            return str(result)
        except Exception as e:
            logger.error(f"❌ Error calling tool {tool_name}: {str(e)}")
            return f"Error calling tool {tool_name}: {str(e)}"
    
    def parse_tool_call(self, llm_response: str) -> Optional[tuple[str, Dict[str, Any]]]:
        """Parse tool call from LLM response if present"""
        logger.info(f"🔍 Parsing LLM response for tool calls...")
        
        if "TOOL_CALL:" in llm_response:
            logger.info("✅ Found TOOL_CALL in response")
            parts = llm_response.split("TOOL_CALL:", 1)[1].strip()
            lines = parts.split("\n", 1)
            tool_line = lines[0].strip()
            logger.info(f"📝 Tool call line: {tool_line}")
            
            if "{" in tool_line:
                tool_name = tool_line.split("{")[0].strip()
                json_str = "{" + tool_line.split("{", 1)[1]
                try:
                    arguments = json.loads(json_str)
                    logger.info(f"✅ Parsed tool: {tool_name} with args: {arguments}")
                    return (tool_name, arguments)
                except json.JSONDecodeError as e:
                    logger.error(f"❌ JSON decode error: {e}")
                    pass
            else:
                logger.info(f"✅ Tool call without arguments: {tool_line}")
                return (tool_line, {})
        else:
            logger.info("ℹ️  No tool call found in response")
        
        return None
    
    def call_llm(self, user_message: str, system_prompt: str = "") -> str:
        """Call Ollama LLM with the conversation history"""
        logger.info(f"🤖 Calling LLM (model: {self.model_name})...")
        messages = []
        
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        
        for msg in self.conversation_history:
            messages.append({"role": msg.role, "content": msg.content})
        
        messages.append({"role": "user", "content": user_message})
        
        logger.info(f"📨 Sending {len(messages)} messages to LLM")
        
        try:
            response = ollama.chat(
                model=self.model_name,
                messages=messages
            )
            llm_response = response['message']['content']
            logger.info(f"✅ LLM response received ({len(llm_response)} chars)")
            logger.debug(f"📄 LLM Response: {llm_response[:200]}...")
            return llm_response
        except Exception as e:
            logger.error(f"❌ Error calling LLM: {str(e)}")
            return f"Error calling LLM: {str(e)}"
    
    async def process_message(self, user_message: str) -> str:
        """Process a user message and return the response"""
        tools_info = self.format_tools_for_llm(self.available_tools)
        system_prompt = f"""You are a helpful cloud compliance assistant. You have access to tools that can check AWS cloud compliance and list resources.

{tools_info}

When a user asks a question that requires using a tool, respond with:
TOOL_CALL: tool_name {{"param1": "value1", "param2": "value2"}}

For example:
- If asked to list S3 buckets: TOOL_CALL: list_s3_buckets {{}}
- If asked to check compliance: TOOL_CALL: check_resource_compliance {{"resourceType": "storage", "standard": "SOC2"}}

After receiving tool results, provide a helpful natural language explanation to the user."""
        
        llm_response = self.call_llm(user_message, system_prompt)
        tool_call = self.parse_tool_call(llm_response)
        
        if tool_call:
            tool_name, arguments = tool_call
            tool_result = await self.call_mcp_tool(tool_name, arguments)
            
            follow_up_prompt = f"The tool '{tool_name}' returned:\n{tool_result}\n\nPlease explain these results to the user in a helpful way."
            final_response = self.call_llm(follow_up_prompt, system_prompt)
            
            self.conversation_history.append(Message("user", user_message))
            self.conversation_history.append(Message("assistant", final_response))
            return final_response
        else:
            self.conversation_history.append(Message("user", user_message))
            self.conversation_history.append(Message("assistant", llm_response))
            return llm_response


# Flask app
app = Flask(__name__)
CORS(app)

# Global client
client = None
client_ready = False

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Cloud Compliance Assistant</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0f0f0f;
            color: #e8e8e8;
            height: 100vh;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }
        
        /* Header */
        .header {
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            padding: 1.2rem 2rem;
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
            display: flex;
            align-items: center;
            justify-content: space-between;
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.3);
        }
        
        .logo-section {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }
        
        .logo {
            font-size: 1.8rem;
            filter: drop-shadow(0 0 8px rgba(102, 126, 234, 0.5));
        }
        
        .title {
            font-size: 1.3rem;
            font-weight: 600;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        
        .subtitle {
            font-size: 0.85rem;
            color: #a0a0a0;
            margin-top: 0.15rem;
        }
        
        .status {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            padding: 0.5rem 1rem;
            background: rgba(102, 126, 234, 0.1);
            border: 1px solid rgba(102, 126, 234, 0.3);
            border-radius: 20px;
            font-size: 0.85rem;
        }
        
        .status-dot {
            width: 8px;
            height: 8px;
            background: #4ade80;
            border-radius: 50%;
            animation: pulse 2s infinite;
        }
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        
        /* Main Content */
        .main-container {
            flex: 1;
            display: flex;
            flex-direction: column;
            max-width: 900px;
            width: 100%;
            margin: 0 auto;
            overflow: hidden;
        }
        
        /* Chat Area */
        .chat-container {
            flex: 1;
            overflow-y: auto;
            padding: 2rem 1.5rem;
            scroll-behavior: smooth;
        }
        
        .chat-container::-webkit-scrollbar {
            width: 8px;
        }
        
        .chat-container::-webkit-scrollbar-track {
            background: transparent;
        }
        
        .chat-container::-webkit-scrollbar-thumb {
            background: rgba(255, 255, 255, 0.2);
            border-radius: 4px;
        }
        
        .chat-container::-webkit-scrollbar-thumb:hover {
            background: rgba(255, 255, 255, 0.3);
        }
        
        /* Welcome Screen */
        .welcome-screen {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 100%;
            text-align: center;
            padding: 2rem;
        }
        
        .welcome-icon {
            font-size: 4rem;
            margin-bottom: 1.5rem;
            filter: drop-shadow(0 0 20px rgba(102, 126, 234, 0.4));
        }
        
        .welcome-title {
            font-size: 2rem;
            font-weight: 600;
            margin-bottom: 0.5rem;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        
        .welcome-text {
            font-size: 1rem;
            color: #a0a0a0;
            margin-bottom: 2.5rem;
            max-width: 500px;
        }
        
        .example-prompts {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 1rem;
            width: 100%;
            max-width: 600px;
        }
        
        .example-card {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 12px;
            padding: 1.25rem;
            cursor: pointer;
            transition: all 0.2s ease;
            text-align: left;
        }
        
        .example-card:hover {
            background: rgba(102, 126, 234, 0.15);
            border-color: rgba(102, 126, 234, 0.4);
            transform: translateY(-2px);
        }
        
        .example-icon {
            font-size: 1.5rem;
            margin-bottom: 0.5rem;
        }
        
        .example-title {
            font-size: 0.95rem;
            font-weight: 500;
            color: #e8e8e8;
            margin-bottom: 0.3rem;
        }
        
        .example-desc {
            font-size: 0.8rem;
            color: #888;
        }
        
        /* Messages */
        .message-group {
            margin-bottom: 2rem;
            animation: slideIn 0.3s ease;
        }
        
        @keyframes slideIn {
            from {
                opacity: 0;
                transform: translateY(10px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }
        
        .message-header {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            margin-bottom: 0.75rem;
        }
        
        .avatar {
            width: 32px;
            height: 32px;
            border-radius: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.2rem;
            flex-shrink: 0;
        }
        
        .avatar.user {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        }
        
        .avatar.assistant {
            background: rgba(255, 255, 255, 0.1);
            border: 1px solid rgba(255, 255, 255, 0.2);
        }
        
        .sender-name {
            font-size: 0.9rem;
            font-weight: 600;
            color: #e8e8e8;
        }
        
        .message-content {
            margin-left: 42px;
            font-size: 0.95rem;
            line-height: 1.6;
            color: #d0d0d0;
            white-space: pre-wrap;
            word-wrap: break-word;
        }
        
        .message-group.assistant .message-content {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 12px;
            padding: 1.25rem;
        }
        
        /* Thinking Animation */
        .thinking {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            color: #888;
            font-size: 0.9rem;
            font-style: italic;
        }
        
        .thinking-dots {
            display: flex;
            gap: 0.3rem;
        }
        
        .thinking-dot {
            width: 6px;
            height: 6px;
            background: #667eea;
            border-radius: 50%;
            animation: bounce 1.4s infinite ease-in-out;
        }
        
        .thinking-dot:nth-child(1) { animation-delay: -0.32s; }
        .thinking-dot:nth-child(2) { animation-delay: -0.16s; }
        
        @keyframes bounce {
            0%, 80%, 100% { transform: scale(0); }
            40% { transform: scale(1); }
        }
        
        /* Input Area */
        .input-container {
            padding: 1.5rem;
            background: #1a1a1a;
            border-top: 1px solid rgba(255, 255, 255, 0.1);
        }
        
        .input-wrapper {
            max-width: 900px;
            margin: 0 auto;
            position: relative;
        }
        
        .input-box {
            display: flex;
            align-items: flex-end;
            background: rgba(255, 255, 255, 0.05);
            border: 2px solid rgba(255, 255, 255, 0.1);
            border-radius: 16px;
            padding: 0.75rem 1rem;
            transition: all 0.2s ease;
        }
        
        .input-box:focus-within {
            border-color: rgba(102, 126, 234, 0.5);
            background: rgba(255, 255, 255, 0.08);
        }
        
        #userInput {
            flex: 1;
            background: transparent;
            border: none;
            outline: none;
            color: #e8e8e8;
            font-size: 0.95rem;
            font-family: inherit;
            resize: none;
            max-height: 150px;
            min-height: 24px;
            line-height: 1.5;
        }
        
        #userInput::placeholder {
            color: #666;
        }
        
        .send-button {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border: none;
            width: 36px;
            height: 36px;
            border-radius: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            transition: all 0.2s ease;
            flex-shrink: 0;
            margin-left: 0.75rem;
        }
        
        .send-button:hover:not(:disabled) {
            transform: translateY(-1px);
            box-shadow: 0 4px 12px rgba(102, 126, 234, 0.4);
        }
        
        .send-button:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        
        .send-icon {
            color: white;
            font-size: 1.2rem;
        }
        
        .input-hint {
            font-size: 0.75rem;
            color: #666;
            text-align: center;
            margin-top: 0.75rem;
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="logo-section">
            <div class="logo">☁️</div>
            <div>
                <div class="title">Cloud Compliance Assistant</div>
                <div class="subtitle">Powered by AWS MCP & Llama 3.2</div>
            </div>
        </div>
        <div class="status">
            <div class="status-dot"></div>
            <span>Online</span>
        </div>
    </div>

    <div class="main-container">
        <div class="chat-container" id="chatContainer">
            <div class="welcome-screen" id="welcomeScreen">
                <div class="welcome-icon">☁️</div>
                <div class="welcome-title">Welcome to Cloud Compliance Assistant</div>
                <div class="welcome-text">
                    I can help you check AWS compliance, list resources, and answer questions about your cloud infrastructure.
                </div>
                <div class="example-prompts">
                    <div class="example-card" onclick="sendExample('List my S3 buckets')">
                        <div class="example-icon">🪣</div>
                        <div class="example-title">List S3 Buckets</div>
                        <div class="example-desc">View all your S3 storage buckets</div>
                    </div>
                    <div class="example-card" onclick="sendExample('Check SOC2 compliance for storage')">
                        <div class="example-icon">✅</div>
                        <div class="example-title">Check Compliance</div>
                        <div class="example-desc">Verify SOC2 compliance status</div>
                    </div>
                    <div class="example-card" onclick="sendExample('What compliance standards do you support?')">
                        <div class="example-icon">📋</div>
                        <div class="example-title">Standards</div>
                        <div class="example-desc">View supported compliance frameworks</div>
                    </div>
                    <div class="example-card" onclick="sendExample('List supported resource types')">
                        <div class="example-icon">🔧</div>
                        <div class="example-title">Resource Types</div>
                        <div class="example-desc">See available AWS resources</div>
                    </div>
                </div>
            </div>
        </div>

        <div class="input-container">
            <div class="input-wrapper">
                <div class="input-box">
                    <textarea 
                        id="userInput" 
                        placeholder="Ask about your cloud compliance..."
                        rows="1"
                        onkeydown="handleKeyDown(event)"
                        oninput="autoResize(this)"
                    ></textarea>
                    <button class="send-button" id="sendBtn" onclick="sendMessage()">
                        <span class="send-icon">↑</span>
                    </button>
                </div>
                <div class="input-hint">Press Enter to send, Shift+Enter for new line</div>
            </div>
        </div>
    </div>

    <script>
        let messageCount = 0;

        function hideWelcome() {
            const welcome = document.getElementById('welcomeScreen');
            if (welcome) {
                welcome.style.display = 'none';
            }
        }

        function autoResize(textarea) {
            textarea.style.height = 'auto';
            textarea.style.height = Math.min(textarea.scrollHeight, 150) + 'px';
        }

        function addMessage(role, content) {
            hideWelcome();
            messageCount++;
            
            const chatContainer = document.getElementById('chatContainer');
            const messageGroup = document.createElement('div');
            messageGroup.className = `message-group ${role}`;
            messageGroup.id = `message-${messageCount}`;
            
            const avatar = role === 'user' ? '👤' : '☁️';
            const name = role === 'user' ? 'You' : 'Assistant';
            
            messageGroup.innerHTML = `
                <div class="message-header">
                    <div class="avatar ${role}">${avatar}</div>
                    <div class="sender-name">${name}</div>
                </div>
                <div class="message-content">${content}</div>
            `;
            
            chatContainer.appendChild(messageGroup);
            chatContainer.scrollTop = chatContainer.scrollHeight;
        }

        function updateLastMessage(content) {
            const lastMessage = document.querySelector('.message-group:last-child .message-content');
            if (lastMessage) {
                lastMessage.innerHTML = content;
            }
        }

        function handleKeyDown(event) {
            if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                sendMessage();
            }
        }

        function sendExample(text) {
            document.getElementById('userInput').value = text;
            sendMessage();
        }

        async function sendMessage() {
            const input = document.getElementById('userInput');
            const sendBtn = document.getElementById('sendBtn');
            const message = input.value.trim();
            
            if (!message) return;
            
            addMessage('user', message);
            input.value = '';
            input.style.height = 'auto';
            sendBtn.disabled = true;
            
            addMessage('assistant', `
                <div class="thinking">
                    <span>Thinking</span>
                    <div class="thinking-dots">
                        <div class="thinking-dot"></div>
                        <div class="thinking-dot"></div>
                        <div class="thinking-dot"></div>
                    </div>
                </div>
            `);
            
            try {
                const response = await fetch('/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ message: message })
                });
                
                const data = await response.json();
                updateLastMessage(data.response);
            } catch (error) {
                updateLastMessage(`<span style="color: #ef4444;">Error: ${error.message}</span>`);
            }
            
            sendBtn.disabled = false;
            input.focus();
        }
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/chat', methods=['POST'])
def chat():
    global client, client_ready
    
    if not client_ready:
        return jsonify({'response': 'Client not initialized yet. Please wait...'}), 503
    
    data = request.json
    message = data.get('message', '')
    
    if not message:
        return jsonify({'response': 'Please provide a message'}), 400
    
    future = asyncio.run_coroutine_threadsafe(
        client.process_message(message),
        client.loop
    )
    
    try:
        response = future.result(timeout=300)
        return jsonify({'response': response})
    except Exception as e:
        logger.error(f"❌ Error processing message: {str(e)}")
        return jsonify({'response': f'Error: {str(e)}'}), 500


async def initialize_mcp():
    global client, client_ready
    
    logger.info("🔧 Connecting to MCP server...")
    
    server_params = StdioServerParameters(
        command="docker",
        args=[
            "exec", "-i",
            "cloud-compliance-mcp",
            "java", "-jar", "/app/cloud-compliance-mcp.jar"
        ]
    )
    
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            
            client = CloudComplianceClient()
            client.session = session
            client.loop = asyncio.get_event_loop()
            client.available_tools = tools_result.tools
            client_ready = True
            
            logger.info(f"✅ MCP Connected! {len(client.available_tools)} tools available")
            logger.info("🌐 Web UI available at: http://localhost:5000")
            
            while True:
                await asyncio.sleep(1)


def run_mcp_in_background():
    """Run MCP initialization in a background thread with its own event loop"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(initialize_mcp())
    except KeyboardInterrupt:
        logger.info("🛑 Shutting down MCP connection...")
    finally:
        loop.close()


if __name__ == '__main__':
    try:
        ollama.list()
        logger.info("✅ Ollama is running")
    except Exception:
        logger.error("❌ Error: Ollama is not running. Please start Ollama first:")
        logger.error("   ollama serve")
        exit(1)
    
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=cloud-compliance-mcp", "--format", "{{.Names}}"],
            capture_output=True,
            text=True
        )
        if "cloud-compliance-mcp" not in result.stdout:
            logger.error("❌ Error: MCP server container is not running. Please start it:")
            logger.error("   docker-compose up -d")
            exit(1)
        logger.info("✅ MCP server container is running")
    except Exception as e:
        logger.error(f"❌ Error checking Docker: {e}")
        exit(1)
    
    logger.info("🚀 Initializing Cloud Compliance Assistant...")
    mcp_thread = threading.Thread(target=run_mcp_in_background, daemon=True)
    mcp_thread.start()
    
    import time
    time.sleep(3)
    
    logger.info("🌐 Starting Flask web server...")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)