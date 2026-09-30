# PyAwaish

**PyAwaish** is a Flask-based Python framework that simplifies creating MySQL-powered web applications with dynamic configuration, RESTful APIs, and CRUD operations.

---

## 🚀 Features

- 🔧 Dynamic MySQL configuration via web UI (host, port, user, password, database)
- 💬 Intelligent SQL feedback — every INSERT, UPDATE, DELETE, CREATE, ALTER, DROP, GRANT, COMMIT and more returns a structured report: headline, stats, notes, schema diffs, and MySQL warnings
- 🔍 Real-time query feedback with enhanced error diagnostics (friendly cards for common MySQL errors like 1045, 1049, 2003)
- 🧩 Template rendering (`templates/` support)
- 🔄 Query execution via POST APIs
- ✍️ CRUD operations (Insert, Delete, Update, Fetch)
- 🌐 RESTful Flask endpoints
- 🔐 Environment variable support for secure configuration

---

## 📦 Installation

**Quick install (recommended):**

```bash
pip install PyAwaish
```

**From source:**

```bash
git clone https://github.com/abuawaish/PyAwaish.git
cd PyAwaish
pip install .
```

## 🔑 Environment Variables

```bash
export MYSQL_HOST=localhost
export MYSQL_PORT=3306
export MYSQL_USER=root
export MYSQL_PASSWORD=your_password
export MYSQL_DB=mydatabase
export SECRET_KEY=your_secret_key
```

### `.env` example

```bash
MYSQL_HOST="localhost"
MYSQL_PORT="3306"
MYSQL_USER="root"
MYSQL_PASSWORD="your_password"
MYSQL_DB="mydatabase"
SECRET_KEY="YOUR_SECRET_KEY"
```

---

## ▶️ Usage

```python
from PyAwaish.MysqlApplication import MysqlApplication

if __name__ == "__main__":
    app = MysqlApplication(secret_key="your_secret_key")
    app.execute(debug_mode=True, port_number=8080, host_address="127.0.0.1")
```

### Secret key options

| Method             | Example                                        |
| ------------------ | ---------------------------------------------- |
| `.env` (full path) | `MysqlApplication(secret_key=r"C:\path\.env")` |
| `.env` (local)     | `MysqlApplication(secret_key=".env")`          |
| No key (default)   | `MysqlApplication()`                           |
| Custom string      | `MysqlApplication(secret_key="key")`           |

---

## 🌐 Endpoints

| Endpoint         | Description             |
| ---------------- | ----------------------- |
| `/`              | MySQL config page       |
| `/home`          | Home page               |
| `/config_mysql`  | POST — Configure MySQL  |
| `/execute_query` | POST — Execute SQL/CRUD |

---

## 🧾 Example Query (POST /execute_query)

```json
{
  "operation": "insert",
  "table_name": "users",
  "columns": "name, email",
  "values": "'John Doe', 'john@example.com'"
}
```

Supported operations: `insert`, `delete`, `update`, `fetch_data`, `show_tables` — plus raw SQL via the custom query box on the home page.

---

## 📚 Dependencies

- Flask
- Flask-MySQLdb
- python-dotenv
