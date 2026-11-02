from functools import wraps
import time

# 由 @wf_tool 自动收集，新增工具时无需再改这里
available_functions = {}


def wf_tool(func):
    """工具装饰器：自动注册到 available_functions，并打印调用日志。"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        print(f"[wf_tool] 调用 {func.__name__} args={args} kwargs={kwargs}")
        start = time.time()
        result = func(*args, **kwargs)
        print(f"[wf_tool] {func.__name__} 完成，耗时 {time.time() - start:.3f}s，结果={result}")
        return result

    available_functions[func.__name__] = wrapper
    return wrapper


@wf_tool
def add(a: int, b: int) -> int:
    """将两个数字相加。

    Args:
        a: 第一个加数
        b: 第二个加数
    """
    print("add ->" + str(a) + " + " + str(b))
    return a + b


@wf_tool
def multiply(a: int, b: int) -> int:
    """将两个数字相乘。

    Args:
        a: 被乘数
        b: 乘数
    """
    print("multiply ->" + str(a) + " * " + str(b))
    return a * b
