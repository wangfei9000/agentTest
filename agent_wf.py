from ollama import chat, ChatResponse
import json

# 1. 定义你的工具（函数）
def add(a: int, b: int) -> int:
    """将两个数字相加。"""
    return a + b

def multiply(a: int, b: int) -> int:
    """将两个数字相乘。"""
    return a * b

# 建立一个从工具名到函数的映射，方便动态调用
available_functions = {
    "add": add,
    "multiply": multiply,
}

# 2. 初始化对话历史
messages = [{
    "role": "user",
    "content": "请计算 (11434 + 12341) * 412 的结果。"
}]

# 3. 开始 Agent Loop
print("--- Agent 开始工作 ---")
for iteration in range(10):  # 安全上限：最多循环 10 次，防止失控
    print(f"\n[第 {iteration + 1} 轮] 正在请求模型...")

    response: ChatResponse = chat(
        model='qwen3:8b',
        messages=messages,
        tools=[add, multiply],  # 将工具清单交给模型
        # think=True,           # 可选：让模型输出思考过程（如果模型支持）
    )

    # 将模型的响应（包含它的“思考”或“决策”）加入历史
    messages.append(response.message)

    # 检查模型是否请求调用工具
    if response.message.tool_calls:
        for tool_call in response.message.tool_calls:
            func_name = tool_call.function.name
            func_args = tool_call.function.arguments

            print(f"  🔧 模型请求调用: {func_name}({func_args})")

            # 4. 执行本地函数
            if func_name in available_functions:
                result = available_functions[func_name](**func_args)
                print(f"  ✅ 本地执行结果: {result}")

                # 5. 将工具执行结果作为 "tool" 消息反馈给模型
                messages.append({
                    "role": "tool",
                    "tool_name": func_name,
                    "content": str(result),
                })
    else:
        # 没有 tool_calls，说明 Agent 认为任务完成
        print("\n--- Agent 最终回答 ---")
        print(response.message.content)
        break
else:
    print("\n--- 已达到最大循环次数，强制结束 ---")
    print("最后状态：", messages[-1])
