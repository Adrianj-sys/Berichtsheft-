import sqlite3
conn = sqlite3.connect('../data/predictions.db')
try:
    conn.execute('ALTER TABLE predictions ADD COLUMN source TEXT DEFAULT "ai"')
    conn.commit()
    print('Column added')
except Exception as e:
    print(f'Error: {e}')
conn.close()
