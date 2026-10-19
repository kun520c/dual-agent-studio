"""OFFLINE SIMULATION ONLY. Codex/Cursor wire formats, no network or models.

java_clip* scenarios write only Utf8Clip.java. fixed_window* scenarios write
only FixedWindowLimiter.java. Maven/JUnit is run by the coordinator, not by
this stand-in, and a passing stand-in is not a real model.
"""
import json
import os
from pathlib import Path
import re
import sys
import time

JAVA_RELATIVE = Path('src/main/java/studio/util/Utf8Clip.java')
WINDOW_RELATIVE = Path('src/main/java/studio/limit/FixedWindowLimiter.java')


def agent_java(name):
    path = Path(__file__).resolve().parents[1] / 'fixtures' / 'utf8-clip' / 'agents' / name
    text = path.read_text(encoding='utf-8')
    if 'class Utf8Clip' not in text:
        raise SystemExit('离线替身缺少 Utf8Clip 样本：' + name)
    return text


def agent_window(name):
    path = Path(__file__).resolve().parents[1] / 'fixtures' / 'fixed-window' / 'agents' / name
    text = path.read_text(encoding='utf-8')
    if 'class FixedWindowLimiter' not in text:
        raise SystemExit('离线替身缺少 FixedWindowLimiter 样本：' + name)
    return text


def apply_fixed_window(workspace, scenario, round_index):
    if scenario in {'fixed_window_timeout', 'fixed_window_hold'} and round_index > 1:
        time.sleep(8 if scenario == 'fixed_window_timeout' else 5)
        return
    if scenario == 'fixed_window_pass':
        name = 'correct.java'
    elif scenario == 'fixed_window_wrong':
        name = 'bug_boundary.java' if round_index == 1 else 'bug_limit.java'
    elif scenario in {'fixed_window', 'fixed_window_timeout', 'fixed_window_hold'}:
        name = 'correct.java' if scenario == 'fixed_window' and round_index > 1 else 'bug_boundary.java'
    else:
        raise SystemExit('未知固定窗口离线场景：' + scenario)
    target = workspace / WINDOW_RELATIVE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(agent_window(name), encoding='utf-8', newline='\n')


def apply_java(workspace, scenario, round_index):
    if scenario in {'java_clip_timeout', 'java_clip_hold'} and round_index > 1:
        time.sleep(8 if scenario == 'java_clip_timeout' else 5)
        return
    if scenario == 'java_clip_pass':
        name = 'correct.java'
    elif scenario == 'java_clip_wrong':
        name = 'bug_char.java' if round_index == 1 else 'bug_split.java'
    elif scenario in {'java_clip', 'java_clip_stuck', 'java_clip_timeout', 'java_clip_hold'}:
        name = 'correct.java' if scenario == 'java_clip' and round_index > 1 else 'bug_char.java'
    else:
        raise SystemExit('未知 Java 离线场景：' + scenario)
    target = workspace / JAVA_RELATIVE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(agent_java(name), encoding='utf-8', newline='\n')


kind, args = sys.argv[1], sys.argv[2:]
workspace = Path(args[args.index('-C' if kind == 'codex' else '--workspace') + 1])
prompt = sys.stdin.read() if kind == 'codex' else args[-1]
if kind == 'cursor' and '文件：' in prompt: prompt = Path(prompt.split('文件：')[-1].strip()).read_text(encoding='utf8')
scenario = (workspace / 'scenario.txt').read_text(encoding='utf8').strip()
round_index = int(re.search(r'【轮次】(\d+)', prompt).group(1))
if scenario == 'accept_slow' and kind == 'cursor' and '/ 验收者' in prompt: time.sleep(1.5)
if scenario == 'ui_stream':
    if kind == 'codex':
        print(json.dumps({'type':'item.completed','item':{'id':'fake-public-'+str(round_index),'type':'agent_message','text':'Fake CLI：正在处理隔离样本，将保留公开执行记录。'},'offline_simulation':True},ensure_ascii=False),flush=True)
    time.sleep(12)
if scenario in {'slow','timeout'} and kind == 'codex': time.sleep(1.5 if scenario == 'slow' else 10)
if scenario == 'nonzero': print('offline simulated exit', file=sys.stderr); raise SystemExit(7)
if scenario == 'quota': print('insufficient_quota (offline simulation)', file=sys.stderr); raise SystemExit(8)
if scenario == 'protocol': answer = 'offline invalid protocol'
else:
    java = scenario.startswith('java_clip')
    window = scenario.startswith('fixed_window')
    if kind == 'codex' and window:
        apply_fixed_window(workspace, scenario, round_index)
    elif kind == 'codex' and java:
        apply_java(workspace, scenario, round_index)
    elif kind == 'codex':
        if scenario=='api_python':
            (workspace/'calculator.py').write_text('def add(a, b):\n    return a + b\n',encoding='utf8')
        elif scenario=='manager_chain':
            previous=(workspace/'value.txt').read_text(encoding='utf8')
            if 'CHAIN_B' in prompt:
                if 'A_IMPLEMENTED' not in previous: raise SystemExit('B cannot read accepted A source')
                content='6\nA_IMPLEMENTED\nB_USED_A\n'
            else: content='5\nA_IMPLEMENTED\n'
            (workspace/'value.txt').write_text(content,encoding='utf8')
        else:
            value = '4' if (scenario in {'repair','ui_stream'} and round_index == 1 or
                scenario=='manager_retry' and 'GoalHost retry generation: 2' not in prompt) else '5'
            (workspace / 'value.txt').write_text(value + '\n任务：' + os.environ['DUAL_AGENT_TASK_ID'] + '\n', encoding='utf8')
    acceptor = '/ 验收者' in prompt
    if window and kind == 'codex':
        touched = [WINDOW_RELATIVE.as_posix()]
    elif java and kind == 'codex':
        touched = [JAVA_RELATIVE.as_posix()]
    elif kind == 'codex' and scenario=='api_python':
        touched = ['calculator.py']
    elif kind == 'codex':
        touched = ['value.txt']
    else:
        touched = []
    answer = json.dumps({'summary':'离线模拟 CLI ' + ('修复' if kind == 'codex' and round_index > 1 else kind),
        'details':'受控替身协议测试；真实模型调用 0。' + ('已读取失败验证和审查反馈。' if round_index > 1 else ''),
        'decision':'accept' if acceptor else 'continue', 'files_touched':touched,'risks':[]}, ensure_ascii=False)
if kind == 'codex':
    Path(args[args.index('-o') + 1]).write_text(answer, encoding='utf8')
    print(json.dumps({'type':'thread.started','offline_simulation':True}))
    print(json.dumps({'type':'turn.completed','usage':{'input_tokens':12,'output_tokens':8},'offline_simulation':True}))
else:
    print(json.dumps({'type':'result','result':answer,'is_error':False,'offline_simulation':True}, ensure_ascii=False))
