import sqlite3
import time

def main():
    conn = sqlite3.connect('camera_analytics.db')
    cur = conn.cursor()
    # Count analytics rows for current ad in last 10 minutes
    cur.execute('SELECT COUNT(*) FROM analytics WHERE ts > strftime("%s","now") - 600 AND ad_id = ?', ('new_ad.mp4',))
    print('analytics last 10 min for new_ad.mp4:', cur.fetchone()[0])
    cur.execute('SELECT MAX(ts) FROM analytics WHERE ad_id = ?', ('new_ad.mp4',))
    print('latest ts for new_ad.mp4:', cur.fetchone()[0])
    conn.close()

if __name__ == '__main__':
    main()