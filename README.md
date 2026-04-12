# Tweet Scraper

A comprehensive suite for scraping and managing X (formerly Twitter) data. This project includes both a Python-based backend crawler and a Chrome Extension to intercept browser sessions, enabling reliable graphQL data collection.

## 🚀 Features

- **Authentication Manager** (`1_authenticator.py`): Handles session integration securely without manual password entries.
- **GraphQL Collector** (`2_graphql_collector.py`): Reverses X's internal GraphQL APIs to collect massive datasets effortlessly.
- **Dynamic Paginator** (`3_paginator.py`): Manages cursor-based pagination seamlessly to pull historical user profiles and timeline tweets.
- **Exporter Utility** (`4_exporter.py`): Normalizes data and exports directly to pristine CSV or JSON files.
- **Chrome Extension Hook** (`chrome_extension/`): Captures active browser session cookies and keys automatically.

## 📦 Installation

Ensure you have Python 3.9+ installed and run:

```bash
pip install -r requirements.txt
```

## ⚙️ Configuration & Usage

The application relies on `config.py` for variables.

1. Inject your cookies via the Authenticator or the included Chrome extension.
2. Run the modules step-by-step or hook into the main orchestrator logic.
3. Check the `output/` directory for your JSON and CSV timeline results.

## 🛡 Disclaimer

*This project is built for educational and research purposes only. Make sure you comply with X's Terms of Service when automating requests.*
