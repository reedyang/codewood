# 项目结构说明

## 目录结构
```
smart-shell/
├── src/
│   ├── main.py                    # 主程序入口
│   ├── smart_shell_agent.py       # Smart Shell AI代理
│   ├── knowledge_manager.py       # 知识库管理器
│   ├── history_manager.py         # 历史记录管理器
│   ├── windows_input.py           # Windows输入处理器
│   └── tab_completer.py           # Unix系统Tab补全
├── .smartshell                    # 配置目录
|   ├── config.json                # 配置文件
|   ├── knowledge/                 # 知识库文档目录
|   ├── knowledge_db/              # 知识库数据库（自动生成）
|   └── knowledge_status.json      # 知识库状态记录（自动生成）
├── demo/                          # 演示文件
├── test_knowledge.py              # 知识库功能测试脚本
├── install_knowledge.py           # 知识库依赖安装脚本
└── README.md                      # 项目说明
```

## 使用方法

### 1. 运行 Smart Shell
```bash
python src/main.py       # 使用默认AI模型
python src/main.py model # 使用指定的AI模型
```

## 新功能特性

### 🔀 目录切换功能
- 支持相对路径和绝对路径切换
- 智能路径验证
- 动态提示符显示当前目录

### 🧠 操作结果反馈
- 记录所有操作结果
- 将结果传递给AI分析
- 提供基于结果的智能建议
- 支持上下文理解

### 📚 知识库功能
- 自动索引文档目录中的文件
- 支持多种文档格式（TXT、PDF、DOCX、MD等）
- 基于向量数据库的语义搜索
- 智能上下文增强，提高AI回答准确性
- 自动检测文档变化并更新索引

## 依赖要求
- Python 3.7+
- ollama Python包
- 本地Ollama服务运行
- 可用的语言模型（如gemma3:4b）

## 知识库依赖（可选）
- chromadb>=0.4.0
- langchain>=0.1.0
- langchain-community>=0.0.10
- sentence-transformers>=2.2.0
- python-multipart>=0.0.6
- Ollama服务（用于向量化）

## 安装依赖
```bash
# 基础依赖
pip install ollama

# 知识库依赖（可选）
python install_knowledge.py
```

## 启动Ollama服务
```bash
ollama serve
```

## 下载模型
```bash
ollama pull gemma3:4b
``` 
