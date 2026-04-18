import sqlite3
import threading
import os
import shutil
import csv
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

class Database:
    _local = threading.local()

    def __init__(self, db_path='telegram_bot.db'):
        self.db_path = db_path
        self._init_lock = threading.Lock()
        self._init_database()

    def _get_conn(self):
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path, timeout=10)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA busy_timeout=5000")
        return self._local.conn

    def _init_database(self):
        with self._init_lock:
            conn = sqlite3.connect(self.db_path, timeout=10)
            c = conn.cursor()

            c.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    plan TEXT DEFAULT 'FREE',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            c.execute('''
                CREATE TABLE IF NOT EXISTS user_queries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    message_id INTEGER,
                    query_text TEXT NOT NULL,
                    query_type TEXT,
                    bot_response TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    chat_id INTEGER,
                    status TEXT DEFAULT 'pending',
                    gateway TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            ''')

            c.execute('''
                CREATE TABLE IF NOT EXISTS card_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    card_bin TEXT,
                    gateway TEXT,
                    result TEXT,
                    response_detail TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    execution_time REAL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            ''')

            c.execute('''
                CREATE TABLE IF NOT EXISTS vip_grants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    days INTEGER DEFAULT 30,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expiry TEXT,
                    is_active INTEGER DEFAULT 1
                )
            ''')

            c.execute('''
                CREATE TABLE IF NOT EXISTS bandc_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL,
                    username TEXT,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            c.execute('CREATE INDEX IF NOT EXISTS idx_queries_user_id ON user_queries(user_id)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_queries_timestamp ON user_queries(timestamp)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_queries_type ON user_queries(query_type)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_cards_user_id ON card_checks(user_id)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_cards_gateway ON card_checks(gateway)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_cards_result ON card_checks(result)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_vip_user_id ON vip_grants(user_id)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_vip_active ON vip_grants(is_active)')

            conn.commit()
            conn.close()
            logger.info("Database initialized successfully")

    def save_user(self, user_id, username=None, first_name=None, last_name=None, plan=None):
        try:
            conn = self._get_conn()
            if plan:
                conn.execute('''
                    INSERT INTO users (user_id, username, first_name, last_name, plan, last_active)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id) DO UPDATE SET
                        username=excluded.username,
                        first_name=excluded.first_name,
                        last_name=excluded.last_name,
                        plan=excluded.plan,
                        last_active=CURRENT_TIMESTAMP
                ''', (user_id, username, first_name, last_name, plan))
            else:
                conn.execute('''
                    INSERT INTO users (user_id, username, first_name, last_name, last_active)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id) DO UPDATE SET
                        username=excluded.username,
                        first_name=excluded.first_name,
                        last_name=excluded.last_name,
                        last_active=CURRENT_TIMESTAMP
                ''', (user_id, username, first_name, last_name))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error saving user: {e}")
            return False

    def get_user_id_by_username(self, username):
        """Return user_id for a given username (with or without @). None if not found."""
        try:
            uname = username.lstrip('@').lower()
            conn = self._get_conn()
            cursor = conn.execute(
                'SELECT user_id FROM users WHERE LOWER(username) = ?', (uname,))
            row = cursor.fetchone()
            return row[0] if row else None
        except Exception:
            return None

    def save_query(self, user_id, message_id, query_text, query_type='command', chat_id=None, bot_response=None, gateway=None):
        try:
            conn = self._get_conn()
            cursor = conn.execute('''
                INSERT INTO user_queries
                (user_id, message_id, query_text, query_type, chat_id, bot_response, status, gateway)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, message_id, query_text, query_type, chat_id,
                  bot_response, 'answered' if bot_response else 'pending', gateway))
            conn.commit()
            return cursor.lastrowid
        except Exception as e:
            logger.error(f"Error saving query: {e}")
            return None

    def update_query_response(self, query_id, bot_response):
        try:
            conn = self._get_conn()
            conn.execute('''
                UPDATE user_queries
                SET bot_response = ?, status = 'answered'
                WHERE id = ?
            ''', (bot_response, query_id))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error updating response: {e}")
            return False

    def save_card_check(self, user_id, card_bin, gateway, result, response_detail=None, execution_time=None):
        try:
            conn = self._get_conn()
            conn.execute('''
                INSERT INTO card_checks
                (user_id, card_bin, gateway, result, response_detail, execution_time)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (user_id, card_bin, gateway, result, response_detail, execution_time))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error saving card check: {e}")
            return False

    def get_user_queries(self, user_id, limit=10):
        try:
            conn = self._get_conn()
            cursor = conn.execute('''
                SELECT query_text, bot_response, timestamp, query_type
                FROM user_queries
                WHERE user_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            ''', (user_id, limit))
            return cursor.fetchall()
        except Exception as e:
            logger.error(f"Error fetching queries: {e}")
            return []

    def get_user_card_checks(self, user_id, limit=10):
        try:
            conn = self._get_conn()
            cursor = conn.execute('''
                SELECT card_bin, gateway, result, timestamp, execution_time
                FROM card_checks
                WHERE user_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            ''', (user_id, limit))
            return cursor.fetchall()
        except Exception as e:
            logger.error(f"Error fetching card checks: {e}")
            return []

    def get_all_queries_count(self):
        try:
            conn = self._get_conn()
            cursor = conn.execute('SELECT COUNT(*) FROM user_queries')
            return cursor.fetchone()[0]
        except:
            return 0

    def get_user_count(self):
        try:
            conn = self._get_conn()
            cursor = conn.execute('SELECT COUNT(*) FROM users')
            return cursor.fetchone()[0]
        except:
            return 0

    def get_card_checks_count(self):
        try:
            conn = self._get_conn()
            cursor = conn.execute('SELECT COUNT(*) FROM card_checks')
            return cursor.fetchone()[0]
        except:
            return 0

    def get_today_stats(self):
        try:
            conn = self._get_conn()
            today = datetime.now().strftime('%Y-%m-%d')

            cursor = conn.execute(
                "SELECT COUNT(*) FROM user_queries WHERE DATE(timestamp) = ?", (today,))
            today_queries = cursor.fetchone()[0]

            cursor = conn.execute(
                "SELECT COUNT(*) FROM card_checks WHERE DATE(timestamp) = ?", (today,))
            today_checks = cursor.fetchone()[0]

            cursor = conn.execute(
                "SELECT COUNT(DISTINCT user_id) FROM user_queries WHERE DATE(timestamp) = ?", (today,))
            today_active = cursor.fetchone()[0]

            cursor = conn.execute(
                "SELECT COUNT(*) FROM card_checks WHERE DATE(timestamp) = ? AND (result LIKE '%Approved%' OR result LIKE '%Charged%' OR result LIKE '%Success%')", (today,))
            today_approved = cursor.fetchone()[0]

            return {
                'queries': today_queries,
                'checks': today_checks,
                'active_users': today_active,
                'approved': today_approved
            }
        except Exception as e:
            logger.error(f"Error getting today stats: {e}")
            return {'queries': 0, 'checks': 0, 'active_users': 0, 'approved': 0}

    def get_gateway_stats(self):
        try:
            conn = self._get_conn()
            cursor = conn.execute('''
                SELECT gateway, COUNT(*) as total,
                    SUM(CASE WHEN result LIKE '%Approved%' OR result LIKE '%Charged%' OR result LIKE '%Success%' THEN 1 ELSE 0 END) as approved
                FROM card_checks
                WHERE gateway IS NOT NULL
                GROUP BY gateway
                ORDER BY total DESC
            ''')
            return cursor.fetchall()
        except Exception as e:
            logger.error(f"Error getting gateway stats: {e}")
            return []

    def get_top_users(self, limit=10):
        try:
            conn = self._get_conn()
            cursor = conn.execute('''
                SELECT u.user_id, u.username, u.first_name, u.plan,
                    COUNT(q.id) as query_count
                FROM users u
                LEFT JOIN user_queries q ON u.user_id = q.user_id
                GROUP BY u.user_id
                ORDER BY query_count DESC
                LIMIT ?
            ''', (limit,))
            return cursor.fetchall()
        except Exception as e:
            logger.error(f"Error getting top users: {e}")
            return []

    def search_queries(self, search_term, user_id=None):
        try:
            conn = self._get_conn()
            if user_id:
                cursor = conn.execute('''
                    SELECT query_text, bot_response, timestamp
                    FROM user_queries
                    WHERE user_id = ? AND query_text LIKE ?
                    ORDER BY timestamp DESC
                    LIMIT 20
                ''', (user_id, f'%{search_term}%'))
            else:
                cursor = conn.execute('''
                    SELECT user_id, query_text, bot_response, timestamp
                    FROM user_queries
                    WHERE query_text LIKE ?
                    ORDER BY timestamp DESC
                    LIMIT 20
                ''', (f'%{search_term}%',))
            return cursor.fetchall()
        except Exception as e:
            logger.error(f"Search error: {e}")
            return []

    def export_to_csv(self, user_id=None, filename='queries_export.csv'):
        try:
            conn = self._get_conn()
            if user_id:
                cursor = conn.execute(
                    'SELECT * FROM user_queries WHERE user_id = ? ORDER BY timestamp DESC', (user_id,))
            else:
                cursor = conn.execute('SELECT * FROM user_queries ORDER BY timestamp DESC')

            rows = cursor.fetchall()
            with open(filename, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(['ID', 'User ID', 'Message ID', 'Query', 'Type',
                                 'Response', 'Timestamp', 'Chat ID', 'Status', 'Gateway'])
                writer.writerows(rows)
            return filename
        except Exception as e:
            logger.error(f"CSV export error: {e}")
            return None

    def export_to_json(self, user_id=None, filename='queries_export.json'):
        try:
            conn = self._get_conn()
            if user_id:
                cursor = conn.execute(
                    'SELECT * FROM user_queries WHERE user_id = ? ORDER BY timestamp DESC', (user_id,))
            else:
                cursor = conn.execute('SELECT * FROM user_queries ORDER BY timestamp DESC')

            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            data = [dict(zip(columns, row)) for row in rows]

            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            return filename
        except Exception as e:
            logger.error(f"JSON export error: {e}")
            return None

    def backup_database(self, backup_path='backups/'):
        try:
            if not os.path.exists(backup_path):
                os.makedirs(backup_path)

            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_file = f'{backup_path}backup_{timestamp}.db'
            shutil.copy2(self.db_path, backup_file)

            backups = sorted([f for f in os.listdir(backup_path) if f.endswith('.db')])
            while len(backups) > 7:
                os.remove(os.path.join(backup_path, backups.pop(0)))

            logger.info(f"Backup created: {backup_file}")
            return backup_file
        except Exception as e:
            logger.error(f"Backup error: {e}")
            return None

    def save_vip_grant(self, user_id, username=None, first_name=None, days=30, expiry=None):
        """Record a VIP grant in the database."""
        try:
            conn = self._get_conn()
            conn.execute(
                'UPDATE vip_grants SET is_active = 0 WHERE user_id = ?',
                (user_id,)
            )
            conn.execute('''
                INSERT INTO vip_grants (user_id, username, first_name, days, expiry, is_active)
                VALUES (?, ?, ?, ?, ?, 1)
            ''', (user_id, username, first_name, days, expiry))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error saving VIP grant: {e}")
            return False

    def remove_vip_grant(self, user_id):
        """Mark all VIP grants for a user as inactive."""
        try:
            conn = self._get_conn()
            conn.execute(
                'UPDATE vip_grants SET is_active = 0 WHERE user_id = ?',
                (user_id,)
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error removing VIP grant: {e}")
            return False

    def get_vip_users(self):
        """Return all currently active VIP grants."""
        try:
            conn = self._get_conn()
            cursor = conn.execute('''
                SELECT user_id, username, first_name, days, added_at, expiry
                FROM vip_grants
                WHERE is_active = 1
                ORDER BY added_at DESC
            ''')
            return cursor.fetchall()
        except Exception as e:
            logger.error(f"Error fetching VIP users: {e}")
            return []

    def close(self):
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
