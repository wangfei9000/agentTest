# Python 学习笔记

## 基础语法

Python 基础学习内容包括：变量、数据类型、条件判断、循环、函数和模块。
建议先掌握这些基础，再进入更复杂的主题。

## 函数

函数用于封装重复代码。Python 通过 `def` 定义函数，可以接收参数并返回结果。
例如：

```python
def add(a, b):
    return a + b
```

## 装饰器

装饰器是一种在不修改原函数代码的前提下，给函数增加额外功能的写法。
常见用途包括日志记录、权限校验、计时等。

```python
def log(func):
    def wrapper(*args, **kwargs):
        print("调用函数:", func.__name__)
        return func(*args, **kwargs)
    return wrapper
```

## 学习路线

Python 学习路线可以从基础语法开始，然后学习数据结构、Web 开发或数据分析，
并通过项目练习巩固。
