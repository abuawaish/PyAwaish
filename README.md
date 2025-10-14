# PyAwaish

**PyAwaish** is a Flask-based Python framework that simplifies creating MySQL-powered web applications with dynamic configuration, RESTful APIs, and CRUD operations.

---

## 🚀 Features

- 🔧 Dynamic MySQL configuration via web UI  
- 🧩 Template rendering (`templates/` support)  
- 🔄 Query execution via POST APIs  
- ✍️ CRUD operations (Insert, Delete, Update, Fetch)  
- 🌐 RESTful Flask endpoints  
- 🔐 Environment variable support for secure configuration  

---

## 📦 Installation

```bash
git clone https://github.com/abuawaish/PyAwaish.git
cd PyAwaish
pip install -r requirements.txt
pip install PyAwaish
```

## Set Environment Variables

```bash
export MYSQL_HOST=localhost
export MYSQL_USER=root
export MYSQL_PASSWORD=your_password
export MYSQL_DB=mydatabase
export SECRET_KEY=your_secret_key
```
## ▶ Usage

```python
from PyAwaish.MysqlApplication import MysqlApplication

if __name__ == "__main__":
    app = MysqlApplication(secret_key="your_secret_key")
    app.execute(debug_mode=True, port_number=8080, host_address="127.0.0.1")
```

## 🔑 Secret Key Options

```text
| Method             | Example                                        |
| ------------------ | ---------------------------------------------- |
| `.env` (full path) | `MysqlApplication(secret_key=r"C:\path\.env")` |
| `.env` (local)     | `MysqlApplication(secret_key=".env")`          |
| No key (default)   | `MysqlApplication()`                           |
| Custom string      | `MysqlApplication(secret_key="key")`           |
```

## .env Example:

```bash
SECRET_KEY="YOUR_SECRET_KEY"
```

### 🌐 Endpoints

```bash
| Endpoint         | Description             |
| ---------------- | ----------------------- |
| `/`              | MySQL config page       |
| `/home`          | Home page               |
| `/config_mysql`  | POST - Configure MySQL  |
| `/execute_query` | POST - Execute SQL/CRUD |
```

## 🧾 Example Query (POST /execute_query)

```json
{
  "operation": "insert",
  "table_name": "users",
  "columns": "name, email",
  "values": "'John Doe', 'john@example.com'"
}
```
- Supported Operations: `insert`, `delete`, `update`, `fetch_data`, `show_tables`

## 📚 Dependencies

- Flask

- Flask-MySQLdb

- python-dotenv

## 📦 Quick Installation (Recommended)

```python
pip install PyAwaish
```

## Access the demo here:

[![Watch the video](https://media1.giphy.com/media/v1.Y2lkPTc5MGI3NjExeWVmeDlubnhxNW1jd2J1anVpZnlqNzNtbGtueXM4NWtwMmZ0bnNuMCZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/zi9e985lJwL9E6wc57/giphy.gif)](https://drive.google.com/file/d/1eGId1hPskyZ_Lm1dyFS_oJxF-XFo7KWr/view?usp=sharing)

