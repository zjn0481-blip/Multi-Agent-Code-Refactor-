# 🛠️ MiMo Multi-Agent Code Refactoring System

![Version](https://img.shields.io/badge/version-2.1%20Beta-blue)
![Python](https://img.shields.io/badge/python-3.10+-brightgreen)
![Model](https://img.shields.io/badge/Powered%20by-Xiaomi%20MiMo%20V2.5-orange)

An advanced, concurrent multi-agent framework designed to solve deep code coupling in legacy monolithic repositories using **Long-chain reasoning** and **AST parsing**.

## 🚀 Why This Project?
Traditional RAG approaches fail when dealing with deep state coupling across dozens of files. This system utilizes a multi-agent workflow (Planner, AST Parser, Refactor Coder, and Reviewer) that reads entire module directories at once (often exceeding **100k+ tokens** per payload) to generate decoupled, clean architecture.

## 🧠 Core Architecture
- **AST Parser Agent**: Extracts global dependencies and builds a knowledge graph.
- **Planner Agent**: Performs Long-chain reasoning across files to prevent state breakdown during refactoring.
- **Refactor Agent**: Generates modernized code based on AST rules.
- **Test Agent**: Automatically generates and runs Jest/PyTest suites to ensure zero-regression.

## 📊 Token Consumption & MiMo Integration
Due to the high frequency of long-context interactions and concurrent multi-agent dialogues, the system currently consumes **35M - 50M tokens daily**. 

We are currently **migrating our core endpoints to the Xiaomi MiMo V2.5 Pro model**, leveraging its superior long-context handling and cost-effectiveness. 

> **Note**: For security reasons and API protection, the core runtime engine logic in the `core/` and `agents/` directories is temporarily simplified in this public repository. Full open-source release is planned after MiMo API integration is stabilized.

## 📸 System In Action


<img width="2559" height="1514" alt="屏幕截图 2026-05-12 215356" src="https://github.com/user-attachments/assets/d7657318-f6a9-4060-8b54-c98707f030fb" />


## ⚙️ Quick Start (Beta)
```bash
pip install -r requirements.txt
cp .env.example .env
# Set your MIMO_API_KEY in .env
python run_pipeline.py
