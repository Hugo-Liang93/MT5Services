import sys
sys.path.insert(0, '.')

from src.indicators_v2.engine.dependency_manager import DependencyManager

dm = DependencyManager()

# 定义一些模拟指标函数
def sma_func(bars, period=20):
    closes = [bar.close for bar in bars]
    return {"sma": sum(closes) / len(closes)} if closes else {}

def ema_func(bars, period=20):
    closes = [bar.close for bar in bars]
    return {"ema": sum(closes) / len(closes)} if closes else {}

def rsi_func(bars, period=14):
    return {"rsi": 50.0}

def macd_func(bars, fast=12, slow=26, signal=9):
    return {"macd": 0.1, "signal": 0.05, "hist": 0.05}

# 添加指标（无依赖）
dm.add_indicator("sma20", sma_func, {"period": 20})
dm.add_indicator("ema50", ema_func, {"period": 50})

print("After adding sma20 and ema50:")
print("graph:", dm.graph)
print("reverse_graph:", dm.reverse_graph)

# 添加有依赖的指标
dm.add_indicator("rsi14", rsi_func, {"period": 14}, dependencies=["sma20"])
print("\nAfter adding rsi14 (depends on sma20):")
print("graph:", dm.graph)
print("reverse_graph:", dm.reverse_graph)

dm.add_indicator("macd", macd_func, {"fast": 12, "slow": 26, "signal": 9}, 
                 dependencies=["ema50", "rsi14"])
print("\nAfter adding macd (depends on ema50, rsi14):")
print("graph:", dm.graph)
print("reverse_graph:", dm.reverse_graph)

# 检查依赖
print("\nDependencies:")
for name in ["sma20", "ema50", "rsi14", "macd"]:
    print(f"{name}: {dm.get_dependencies(name)}")

print("\nDependents:")
for name in ["sma20", "ema50", "rsi14", "macd"]:
    print(f"{name}: {dm.get_dependents(name)}")

# 尝试获取执行顺序
try:
    order = dm.get_execution_order(["macd"])
    print(f"\nExecution order for ['macd']: {order}")
except Exception as e:
    print(f"\nError getting execution order: {e}")
    import traceback
    traceback.print_exc()

# 尝试获取所有指标的执行顺序
try:
    order_all = dm.get_execution_order()
    print(f"\nExecution order for all: {order_all}")
except Exception as e:
    print(f"\nError getting all execution order: {e}")
    import traceback
    traceback.print_exc()