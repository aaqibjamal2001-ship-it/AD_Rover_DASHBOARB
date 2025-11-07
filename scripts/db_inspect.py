import sqlite3
from pprint import pprint

def main():
    conn = sqlite3.connect('camera_analytics.db')
    cur = conn.cursor()

    print('== analytics schema ==')
    cur.execute('PRAGMA table_info(analytics)')
    cols = [r[1] for r in cur.fetchall()]
    pprint(cols)

    print('\n== presence_log schema ==')
    cur.execute('PRAGMA table_info(presence_log)')
    pcols = [r[1] for r in cur.fetchall()]
    pprint(pcols)

    print('\n== analytics count ==')
    cur.execute('SELECT COUNT(*) FROM analytics')
    print(cur.fetchone()[0])

    print('\n== presence_log last 10 min ==')
    cur.execute('SELECT COUNT(*) FROM presence_log WHERE end_ts > strftime("%s","now") - 600')
    row = cur.fetchone()
    print(row[0] if row else 0)

    print('\n== latest presence_log rows ==')
    cur.execute('SELECT track_id, ad_id, start_ts, end_ts, duration_sec FROM presence_log ORDER BY id DESC LIMIT 5')
    for r in cur.fetchall():
        print(r)

    conn.close()

if __name__ == '__main__':
    main()