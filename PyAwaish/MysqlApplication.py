import os
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response
from typing import Any
from flask_mysqldb import MySQL
from dotenv import load_dotenv


class MysqlApplication:
    """

    MySQL Application with Flask Integration
    ----------------------------------------

    A Flask-based web application for interacting with MySQL databases. Provides a user-friendly interface
    to configure database connections, execute SQL queries, and perform CRUD operations securely.

    Features:
    ---------

    - Dynamic MySQL connection configuration via web interface
    - Secure environment variable handling for credentials
    - RESTful API endpoints for database operations
    - Real-time query execution with JSON responses
    - Error handling and user feedback with flash messages
    - Support for both raw SQL queries and structured operations

    Initialization Parameters:
    --------------------------

    - secret_key (str):

      - Path to .env file containing 'SECRET_KEY' and database credentials OR
      - Direct secret key string for Flask session encryption
      - If empty, uses default credentials with security warnings

    Routes:
    -------

    - **GET /:** Configuration page for MySQL connection setup
    - **POST /config_mysql:** Handles database configuration form submission
    - **GET /home:** Main interface for executing database operations
    - **POST /execute_query:** Endpoint for processing SQL queries and operations

    Key Methods:
    ------------

    - execute(debug_mode, port_number, host_address): Starts Flask server

      - **debug_mode (bool):** Enable/disable debug mode (default: False)
      - **port_number (int):** Port to run application (default: 5001)
      - **host_address (str):** Network interface binding (default: 0.0.0.0)

    Supported Operations via /execute_query:
    ----------------------------------------

    - Raw SQL queries (SELECT, INSERT, UPDATE, DELETE, CALL, etc.)
    - Structured operations:

      - **insert:** Add records with table/columns/values
      - **delete:** Remove records with table/condition
      - **update:** Modify records with table/field/condition
      - **fetch_data:** Retrieve all data from table
      - **show_tables:** List all tables in database

    Security Features:
    ------------------

    - Environment variable isolation
    - Connection validation before configuration
    - Parameterized query execution (via structured operations)
    - Secure secret key handling with fallback warnings

    Dependencies:
    -------------

    - Flask web framework
    - flask-mysqldb for MySQL integration
    - python-dotenv for environment management

    Example Usage:
    ---------------

    >>> app = MysqlApplication(secret_key="your_secret_key_or_env_path")
    >>> app.execute(debug_mode=False, port=5000)

    """

    def __init__(self , secret_key: str  = "") -> None:
        self.__secret_key = secret_key.strip()
        self.__load_environment(self.__secret_key)  # Load environment or set secret key

        # Initialize Flask app
        self.__app = Flask(__name__)
        self.__app.secret_key = self.__secret_key
        self.__database_name = ""

        # Securely load database configuration
        self.__app.config['MYSQL_HOST'] = os.getenv('MYSQL_HOST', 'localhost')
        self.__app.config['MYSQL_USER'] = os.getenv('MYSQL_USER', 'root')
        self.__app.config['MYSQL_PASSWORD'] = os.getenv('MYSQL_PASSWORD', '')
        self.__app.config['MYSQL_DB'] = os.getenv('MYSQL_DB', '')

        # Initialize MySQL
        self.__mysql = MySQL(self.__app)

        # Define routes
        self.__add_config_routes()
        self.__add_home_routes()
        self.__add_config_mysql()
        self.__add_execute_query()

    def __load_environment(self, env_input: str) -> None:
        """
        Handles loading the .env file if the user provides a path or filename.
        If a random string is provided, it's used directly as a secret key.
        """
        self.__env_path = None  # Initialize as None

        if env_input == "":
            print(f"⚠️ Warning: No secret key is provided. Using default secret key.")
            self.__secret_key = "default_secret_key"
            return

        if env_input:  # If user provides input
            if os.path.isabs(env_input) or os.path.exists(env_input):
                self.__env_path = env_input  # Absolute path or existing relative path
            elif os.path.exists(os.path.join(os.getcwd(), env_input)):
                self.__env_path = os.path.join(os.getcwd(), env_input)  # File in current dir
            else:
                print(f"🔐 Using provided secret key: {env_input}")
                self.__secret_key = env_input
                return

        else:  # If no secret key is provided
            default_env_path = os.path.join(os.getcwd(), ".env")
            if os.path.exists(default_env_path):  # Only load if .env exists
                self.__env_path = default_env_path

        # Load environment variables if a valid file is found
        if self.__env_path and os.path.exists(self.__env_path):
            load_dotenv(self.__env_path)
            secret_key = os.getenv("SECRET_KEY")
            if secret_key:
                self.__secret_key = secret_key
                print(f"✅ Loaded environment variables from {self.__env_path}")
            else:
                print(f"⚠️ Failed to load SECRET_KEY from '{self.__env_path}' The app runs with the default secret key.")
                self.__secret_key = "default_secret_key"
        else:
            print(f"⚠️ Warning: No .env file found. Using default secret key.")
            self.__secret_key = "default_secret_key"


    def __add_config_routes(self) -> None:
        @self.__app.route('/')
        def config_mysql_page() -> Any:
            return render_template('config_mysql.html')

    def __add_home_routes(self) -> None:
        @self.__app.route('/home')
        def home() -> Any:
            return render_template('home.html')

    def __add_config_mysql(self) -> None:
        @self.__app.route('/config_mysql', methods=['POST'])
        def config_mysql() -> Response:
            host = request.form['host']
            username = request.form['username']
            password = request.form['password']
            database = request.form['database']

            # Update app configuration dynamically
            self.__app.config['MYSQL_HOST'] = host
            self.__app.config['MYSQL_USER'] = username
            self.__app.config['MYSQL_PASSWORD'] = password
            self.__app.config['MYSQL_DB'] = database
            self.__database_name = database

            try:
                # Test the connection
                cursor = self.__mysql.connection.cursor()
                cursor.execute('SELECT 1')
                cursor.close()
                flash('Connection established successfully!', 'success')
                return redirect(url_for('home'))
            except Exception as e:
                flash(f'Error connecting to MySQL: {e}', 'danger')
                return redirect(url_for('config_mysql_page'))

    def __add_execute_query(self) -> None:
        @self.__app.route('/execute_query', methods=['POST'])
        def execute_query() -> jsonify:
            try:
                data = request.get_json()
                operation = data.get('operation')
                query = data.get('query')
                result: dict[str, str | dict[Any, Any]] = {}

                cursor = self.__mysql.connection.cursor()

                if query:
                    query_type = query.strip().split(' ', 1)[0].lower()

                    if query_type in ['select', 'show', 'describe', 'explain', 'with']:
                        cursor.execute(query)
                        rows = cursor.fetchall()
                        column_names = [desc[0] for desc in cursor.description or []]
                        if rows:
                            result["data"] = {f"row_{index + 1}": dict(zip(column_names, row)) for index, row in enumerate(rows)}
                        else:
                            result["message"] = "No data found."

                    elif query_type == 'call':
                        cursor.execute(query)
                        rows = cursor.fetchall()
                        column_names = [desc[0] for desc in cursor.description or []]
                        if rows:
                            result["data"] = {f"row_{index + 1}": dict(zip(column_names, row)) for index, row in enumerate(rows)}
                        else:
                            self.__mysql.connection.commit()
                            result["message"] = f"{query_type.upper()} statement executed successfully."

                    elif query_type == 'use':
                        try:
                            tokens = query.strip().rstrip(';').split()
                            if len(tokens) < 2:
                                result["message"] = "Invalid USE query: missing database name."
                            else:
                                requested_db = tokens[1].lower()
                                current_db = self.__database_name.lower()

                                if requested_db == current_db:
                                    result["message"] = "You are already using this database."
                                else:
                                    result["message"] = "Cannot switch databases dynamically. Please reconfigure"
                        except Exception as e:
                            return jsonify({'error': f'{e}'})

                    elif query_type == 'help':
                        return jsonify({"warning": "The help command is not supported yet"})

                    else:
                        cursor.execute(query)
                        self.__mysql.connection.commit()
                        result["message"] = f"{query_type.upper()} statement executed successfully."

                elif operation:
                    if operation == "insert":
                        table_name = data.get('table_name')
                        columns = data.get('columns')
                        values = data.get('values')
                        if not table_name or not columns or not values:
                            return jsonify({"warning": "Table name, columns, and values are required for insert"}), 400
                        query = f"INSERT INTO {table_name} ({columns}) VALUES ({values})"
                        cursor.execute(query)
                        self.__mysql.connection.commit()
                        result["message"] = f"Data inserted successfully into table '{table_name}'"

                    elif operation == "delete":
                        table_name = data.get('table_name')
                        condition = data.get('condition')
                        if not table_name or not condition:
                            return jsonify({"warning": "Table name and condition are required for delete"}), 400
                        query = f"DELETE FROM {table_name} WHERE {condition}"
                        cursor.execute(query)
                        self.__mysql.connection.commit()
                        result["message"] = f"Data deleted successfully from table '{table_name}'"

                    elif operation == "update":
                        table_name = data.get('table_name')
                        field = data.get('field')
                        condition = data.get('condition')
                        if not table_name or not field or not condition:
                            return jsonify({"warning": "Table name, field, and condition are required for update"}), 400
                        query = f"UPDATE {table_name} SET {field} WHERE {condition}"
                        cursor.execute(query)
                        self.__mysql.connection.commit()
                        result["message"] = f"Data updated successfully in table '{table_name}'"

                    elif operation == "fetch_data":
                        table_name = data.get('table_name')
                        if not table_name:
                            return jsonify({"warning": "Table name is required for fetch"}), 400
                        query = f'SELECT * FROM {table_name}'
                        cursor.execute(query)
                        rows = cursor.fetchall()
                        column_names = [desc[0] for desc in cursor.description or []]
                        if rows:
                            result["data"] = {f"row_{index + 1}": dict(zip(column_names, row)) for index, row in enumerate(rows)}
                        else:
                            result["message"] = "No data found."

                    elif operation == "show_tables":
                        cursor.execute("SHOW TABLES;")
                        tables = cursor.fetchall()
                        if tables:
                            result["tables"] = {f"table_{index + 1}": table[0] for index, table in enumerate(tables)}
                        else:
                            result['message'] = "No tables found."
                    else:
                        return jsonify({"error": "Invalid operation"}), 400

                else:
                    return jsonify({"warning": "Custom query is required"}), 400

                cursor.close()

                return jsonify(result)

            except Exception as e:
                return jsonify({'error': f'{e}'}), 500

    def execute(self,debug_mode: bool = False , port_number: int = 5001 , host_address: str = "0.0.0.0") -> None:
        self.__app.run(debug=debug_mode, port=port_number, host=host_address)
