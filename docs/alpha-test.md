# Alpha Test Guide

适用于第一批外部测试者的最小验证流程。  
目标：验证安装、可用性、任务执行稳定性和效率报告可读性。

## 测试者清单

1. 安装者（测试者本人）
2. 所测设备（macOS 环境）
3. 用于真实任务验证的仓库

## 测试步骤

### 1) 安装

```bash
cd /path/to/token-aware-orchestrator-v0-3-1
python3 scripts/orchestratorctl.py install
```

要求截图/记录：

- `READY` 与 `OPTIONAL` 区块
- 输出中的 `skill_root`、`profile`

### 2) 运行 status

```bash
token-aware-orchestrator status
```

要求截图/记录：

- `System Status`
- `System` / `Core` / `Optional` 状态
- 若有 WARN，记录对应项与原因

### 3) 使用真实 Codex 任务

任选一个真实任务（非演示任务）：

- 典型类型：文件修改、日志定位、重构补丁
- 记录：
  - 任务类型（bug fix / refactor / locate）
  - 是否遇到 fallback
  - 任务结果主观可接受性（成功/失败）

### 4) 运行 report

```bash
token-aware-orchestrator report
```

要求截图/记录：

- `AI Efficiency Report`
- `Context` 区域
- `Quality` 区域
- `Execution` 区域（特别是 Local Worker/Fallback）

### 5) 反馈

请按以下格式提交：

- 系统  
  - macOS 版本  
  - shell（zsh/bash）  
  - 是否开启 Ollama  
- 任务类型  
  - 任务描述  
  - 结果（成功/失败）  
- Context变化  
  - Before / After / Reduction  
- 是否遇到问题  
  - 命令输出  
  - 你认为的根因  
  - 建议修复点  

## 通过标准（建议）

- install / status 都可正常运行  
- 不出现 ERROR（允许少量 WARN）  
- 真实任务可完成且可理解报告输出  
- report 能返回 Task、Context、Quality、Execution 的完整区块
