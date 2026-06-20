import mysql.connector
from config import DATABASE_CONFIG, DB_NAME


def drop_n_create_database(db_name):
    conn = mysql.connector.connect(**DATABASE_CONFIG)
    cur = conn.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS {db_name};")
    cur.execute(f"CREATE DATABASE IF NOT EXISTS {db_name};")
    conn.commit()
    cur.close()
    conn.close()
    print(f"database {db_name} created successfully")


def create_table_user(database_name):
    conn = mysql.connector.connect(**DATABASE_CONFIG, database=database_name)
    cur = conn.cursor()
    SQL_Query = """
    CREATE TABLE users (
        id         INT AUTO_INCREMENT PRIMARY KEY,
        cid        BIGINT UNIQUE,
        name       VARCHAR(100),
        username   VARCHAR(50),
        is_banned  BOOLEAN DEFAULT FALSE,
        ban_reason VARCHAR(255),
        added_by   BIGINT,
        created_at DATETIME DEFAULT NOW()
    );
    """
    cur.execute(SQL_Query)
    conn.commit()
    cur.close()
    conn.close()
    print("table users created")


def create_table_admin(database_name):
    conn = mysql.connector.connect(**DATABASE_CONFIG, database=database_name)
    cur = conn.cursor()
    SQL_Query = """
    CREATE TABLE admins (
        id         INT AUTO_INCREMENT PRIMARY KEY,
        cid        BIGINT UNIQUE,
        name       VARCHAR(100),
        role       ENUM('admin', 'superadmin'),
        created_at DATETIME DEFAULT NOW()
    );
    """
    cur.execute(SQL_Query)
    conn.commit()
    cur.close()
    conn.close()
    print("table admins created")


def create_table_model(database_name):
    conn = mysql.connector.connect(**DATABASE_CONFIG, database=database_name)
    cur = conn.cursor()
    SQL_Query = """
    CREATE TABLE models (
        id         INT AUTO_INCREMENT PRIMARY KEY,
        name       VARCHAR(100),
        is_active  BOOLEAN DEFAULT TRUE,
        created_at DATETIME DEFAULT NOW()
    );
    """
    cur.execute(SQL_Query)
    conn.commit()
    cur.close()
    conn.close()
    print("table models created")


def create_table_model_prices(database_name):
    conn = mysql.connector.connect(**DATABASE_CONFIG, database=database_name)
    cur = conn.cursor()
    SQL_Query = """
    CREATE TABLE model_prices (
        id       INT AUTO_INCREMENT PRIMARY KEY,
        model_id INT        NOT NULL,
        price    DECIMAL(12, 0) NOT NULL,
        set_at   DATETIME DEFAULT NOW(),
        set_by   INT,
        FOREIGN KEY (model_id) REFERENCES models (id),
        FOREIGN KEY (set_by)   REFERENCES admins (id)
    );
    """
    cur.execute(SQL_Query)
    conn.commit()
    cur.close()
    conn.close()
    print("table model_prices created")


def create_table_orders(database_name):
    """
    یک سفارش = یک فاکتور کلی برای یک کاربر.
    آیتم‌های هر سفارش (مدل‌ها) در جدول order_items نگه‌داری می‌شن.
    total_price = جمع قیمت همه آیتم‌ها (محاسبه و ذخیره هنگام ثبت).
    """
    conn = mysql.connector.connect(**DATABASE_CONFIG, database=database_name)
    cur = conn.cursor()
    SQL_Query = """
    CREATE TABLE orders (
        id               INT AUTO_INCREMENT PRIMARY KEY,
        user_id          INT            NOT NULL,
        total_price      DECIMAL(12, 0) NOT NULL DEFAULT 0,
        fabric           TEXT,
        delivery_date    DATE,
        note             TEXT,
        status           ENUM(
                             'pending',
                             'approved',
                             'producing',
                             'ready',
                             'delivered',
                             'rejected'
                         ) DEFAULT 'pending',
        rejection_reason VARCHAR(255),
        created_at       DATETIME DEFAULT NOW(),
        updated_at       DATETIME DEFAULT NOW() ON UPDATE NOW(),
        FOREIGN KEY (user_id) REFERENCES users (id)
    );
    """
    cur.execute(SQL_Query)
    conn.commit()
    cur.close()
    conn.close()
    print("table orders created")


def create_table_order_items(database_name):
    """
    هر ردیف = یک مدل در یک سفارش.
    unit_price: قیمت مدل در لحظه ثبت سفارش (تاریخچه‌دار).
    line_total = quantity × unit_price (محاسبه و ذخیره هنگام ثبت).
    """
    conn = mysql.connector.connect(**DATABASE_CONFIG, database=database_name)
    cur = conn.cursor()
    SQL_Query = """
    CREATE TABLE order_items (
        id         INT AUTO_INCREMENT PRIMARY KEY,
        order_id   INT            NOT NULL,
        model_id   INT            NOT NULL,
        quantity   INT            NOT NULL,
        unit_price DECIMAL(12, 0) NOT NULL,
        line_total DECIMAL(12, 0) NOT NULL,
        FOREIGN KEY (order_id) REFERENCES orders (id) ON DELETE CASCADE,
        FOREIGN KEY (model_id) REFERENCES models  (id)
    );
    """
    cur.execute(SQL_Query)
    conn.commit()
    cur.close()
    conn.close()
    print("table order_items created")


def create_table_user_finance(database_name):
    conn = mysql.connector.connect(**DATABASE_CONFIG, database=database_name)
    cur = conn.cursor()
    SQL_Query = """
    CREATE TABLE user_finance (
        id                 INT AUTO_INCREMENT PRIMARY KEY,
        user_id            INT UNIQUE     NOT NULL,
        current_month_debt DECIMAL(12, 0) DEFAULT 0,
        previous_debt      DECIMAL(12, 0) DEFAULT 0,
        FOREIGN KEY (user_id) REFERENCES users (id)
    );
    """
    cur.execute(SQL_Query)
    conn.commit()
    cur.close()
    conn.close()
    print("table user_finance created")


def create_table_transactions(database_name):
    conn = mysql.connector.connect(**DATABASE_CONFIG, database=database_name)
    cur = conn.cursor()
    SQL_Query = """
    CREATE TABLE transactions (
        id               INT AUTO_INCREMENT PRIMARY KEY,
        user_id          INT            NOT NULL,
        type             ENUM(
                             'order',
                             'payment',
                             'debt_added',
                             'installment',
                             'month_transfer'
                         ) NOT NULL,
        amount           DECIMAL(12, 0) NOT NULL,
        description      TEXT,
        status           ENUM('pending', 'approved', 'rejected') DEFAULT NULL,
        receipt_file_id  VARCHAR(255)   DEFAULT NULL,
        created_at       DATETIME DEFAULT NOW(),
        created_by       INT,
        FOREIGN KEY (user_id)    REFERENCES users  (id),
        FOREIGN KEY (created_by) REFERENCES admins (id)
    );
    """
    cur.execute(SQL_Query)
    conn.commit()
    cur.close()
    conn.close()
    print("table transactions created")


def create_table_installments(database_name):
    conn = mysql.connector.connect(**DATABASE_CONFIG, database=database_name)
    cur = conn.cursor()
    SQL_Query = """
    CREATE TABLE installments (
        id                INT AUTO_INCREMENT PRIMARY KEY,
        user_id           INT            NOT NULL,
        total_amount      DECIMAL(12, 0) NOT NULL,
        paid_amount       DECIMAL(12, 0) DEFAULT 0,
        num_installments  INT            NOT NULL,
        per_installment   DECIMAL(12, 0) NOT NULL,
        due_dates         JSON,
        created_at        DATETIME DEFAULT NOW(),
        created_by        INT,
        FOREIGN KEY (user_id)    REFERENCES users  (id),
        FOREIGN KEY (created_by) REFERENCES admins (id)
    );
    """
    cur.execute(SQL_Query)
    conn.commit()
    cur.close()
    conn.close()
    print("table installments created")


if __name__ == "__main__":
    drop_n_create_database(DB_NAME)
    create_table_user(DB_NAME)
    create_table_admin(DB_NAME)
    create_table_model(DB_NAME)
    create_table_model_prices(DB_NAME)
    create_table_orders(DB_NAME)
    create_table_order_items(DB_NAME)       # ← جدید
    create_table_user_finance(DB_NAME)
    create_table_transactions(DB_NAME)
    create_table_installments(DB_NAME)
