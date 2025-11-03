# mcp-client/mcp_client.py

import asyncio
import json
import subprocess
from typing import Optional, Dict, Any, List
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import ollama
from dataclasses import dataclass


@dataclass
class Message:
    role: str
    content: str


class CloudComplianceClient:
    def __init__(self, model_name: str = "llama3.2:3b"):
        self.model_name = model_name
        self.conversation_history: List[Message] = []
        self.available_tools: List[Dict] = []
        
    async def connect_to_mcp_server(self):
        """Connect to the MCP server running in Docker"""
        server_params = StdioServerParameters(
            command="docker",
            args=[
                "exec", "-i", 
                "cloud-compliance-mcp",
                "java", "-jar", "/app/cloud-compliance-mcp.jar"
            ]
        )
        
        return stdio_client(server_params)
    
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
    
    async def call_mcp_tool(self, session: ClientSession, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Call an MCP tool and return the result"""
        try:
            result = await session.call_tool(tool_name, arguments)
            
            # Extract content from result
            if hasattr(result, 'content') and result.content:
                if isinstance(result.content, list) and len(result.content) > 0:
                    return result.content[0].text
                return str(result.content)
            return str(result)
        except Exception as e:
            return f"Error calling tool {tool_name}: {str(e)}"
    
    def parse_tool_call(self, llm_response: str) -> Optional[tuple[str, Dict[str, Any]]]:
        """Parse tool call from LLM response if present"""
        # Look for tool call patterns like:
        # TOOL_CALL: tool_name {"param": "value"}
        # Or JSON format tool calls
        
        if "TOOL_CALL:" in llm_response:
            parts = llm_response.split("TOOL_CALL:", 1)[1].strip()
            lines = parts.split("\n", 1)
            tool_line = lines[0].strip()
            
            # Parse tool name and arguments
            if "{" in tool_line:
                tool_name = tool_line.split("{")[0].strip()
                json_str = "{" + tool_line.split("{", 1)[1]
                try:
                    arguments = json.loads(json_str)
                    return (tool_name, arguments)
                except json.JSONDecodeError:
                    pass
            else:
                # Tool call without arguments
                return (tool_line, {})
        
        return None
    
    def call_llm(self, user_message: str, system_prompt: str = "") -> str:
        """Call Ollama LLM with the conversation history"""
        messages = []
        
        # Add system prompt if provided
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        
        # Add conversation history
        for msg in self.conversation_history:
            messages.append({"role": msg.role, "content": msg.content})
        
        # Add current user message
        messages.append({"role": "user", "content": user_message})
        
        # Call Ollama
        try:
            response = ollama.chat(
                model=self.model_name,
                messages=messages
            )
            return response['message']['content']
        except Exception as e:
            return f"Error calling LLM: {str(e)}"
    
    async def chat_loop(self):
        """Main conversation loop"""
        print("🚀 Cloud Compliance Assistant Starting...")
        print("Connecting to MCP server...\n")
        
        async with await self.connect_to_mcp_server() as (read, write):
            async with ClientSession(read, write) as session:
                # Initialize MCP session
                await session.initialize()
                
                # Get available tools
                tools_result = await session.list_tools()
                self.available_tools = tools_result.tools
                
                print(f"✅ Connected! {len(self.available_tools)} tools available.\n")
                print("Tools loaded:")
                for tool in self.available_tools:
                    print(f"  - {tool.name}")
                print("\n" + "="*60)
                print("You can now ask questions about your AWS cloud compliance!")
                print("Examples:")
                print("  - 'Check SOC2 compliance for storage resources'")
                print("  - 'List my S3 buckets'")
                print("  - 'What compliance standards do you support?'")
                print("Type 'exit' or 'quit' to end the conversation.")
                print("="*60 + "\n")
                
                # Create system prompt with tool information
                tools_info = self.format_tools_for_llm(self.available_tools)
                system_prompt = f"""You are a helpful cloud compliance assistant. You have access to tools that can check AWS cloud compliance and list resources.

{tools_info}

When a user asks a question that requires using a tool, respond with:
TOOL_CALL: tool_name {{"param1": "value1", "param2": "value2"}}

For example:
- If asked to list S3 buckets: TOOL_CALL: list_s3_buckets {{}}
- If asked to check compliance: TOOL_CALL: check_resource_compliance {{"resourceType": "storage", "standard": "SOC2"}}

After receiving tool results, provide a helpful natural language explanation to the user."""
                
                # Conversation loop
                while True:
                    try:
                        user_input = input("\n👤 You: ").strip()
                        
                        if user_input.lower() in ['exit', 'quit', 'bye']:
                            print("\n👋 Goodbye!")
                            break
                        
                        if not user_input:
                            continue
                        
                        # Get LLM response
                        print("\n🤖 Assistant: ", end="", flush=True)
                        llm_response = self.call_llm(user_input, system_prompt)
                        
                        # Check if LLM wants to call a tool
                        tool_call = self.parse_tool_call(llm_response)
                        
                        if tool_call:
                            tool_name, arguments = tool_call
                            print(f"[Calling tool: {tool_name}...]")
                            
                            # Call the tool
                            tool_result = await self.call_mcp_tool(session, tool_name, arguments)
                            
                            # Send tool result back to LLM for natural language response
                            follow_up_prompt = f"The tool '{tool_name}' returned:\n{tool_result}\n\nPlease explain these results to the user in a helpful way."
                            final_response = self.call_llm(follow_up_prompt, system_prompt)
                            print(final_response)
                            
                            # Save to history
                            self.conversation_history.append(Message("user", user_input))
                            self.conversation_history.append(Message("assistant", final_response))
                        else:
                            # Direct response without tool call
                            print(llm_response)
                            
                            # Save to history
                            self.conversation_history.append(Message("user", user_input))
                            self.conversation_history.append(Message("assistant", llm_response))
                    
                    except KeyboardInterrupt:
                        print("\n\n👋 Goodbye!")
                        break
                    except Exception as e:
                        print(f"\n❌ Error: {str(e)}")


async def main():
    # Check if Ollama is running
    try:
        ollama.list()
    except Exception:
        print("❌ Error: Ollama is not running. Please start Ollama first:")
        print("   ollama serve")
        return
    
    # Check if Docker container is running
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=cloud-compliance-mcp", "--format", "{{.Names}}"],
            capture_output=True,
            text=True
        )
        if "cloud-compliance-mcp" not in result.stdout:
            print("❌ Error: MCP server container is not running. Please start it:")
            print("   docker-compose up -d")
            return
    except Exception as e:
        print(f"❌ Error checking Docker: {e}")
        return
    
    client = CloudComplianceClient(model_name="llama3.2:3b")
    await client.chat_loop()


if __name__ == "__main__":
    asyncio.run(main())