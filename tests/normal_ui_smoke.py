"""Exercise normal create/queue/collection controls in a real browser, isolated DB and no QQ."""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from inquiry import runtime, db, isolation, collection, jobs, browser
from ui import server, service
from playwright.sync_api import sync_playwright, expect


def main():
    output=runtime.DATA_DIR/'qa/manual-collection-20261022'
    output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime.TEMP_DIR,prefix='normal-ui-') as directory:
        folder=Path(directory)
        settings=folder/'settings.toml'
        settings.write_text((runtime.ROOT/'settings.example.toml').read_text(encoding='utf-8'),encoding='utf-8')
        env={key:{'ok':True,'text':'已就绪'} for key in ('qq','browser','deepseek','card')}
        with patch.object(db,'DB_PATH',folder/'test.db'),patch.object(collection,'STATE_FILE',folder/'collection.json'),patch.object(server,'SETTINGS_FILE',settings),patch.object(isolation,'ENABLED',True),patch.object(isolation,'MODE','worker'),patch.object(isolation,'ALLOW_SEND',True),patch.object(server,'run_state',return_value={'running':False,'stopping':False}),patch.object(server,'env_status',return_value=env),patch('inquiry.qq.send_text',side_effect=AssertionError('No QQ in browser smoke')),patch.object(server,'spawn',side_effect=AssertionError('No executors in browser smoke')):
            httpd=service.bind_server(server.app,0)
            threading.Thread(target=httpd.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    b=pw.chromium.launch(executable_path=str(next(p for p in browser.EDGE_CANDIDATES if p.exists())),headless=True,args=[f'--disk-cache-dir={folder/"cache"}',f'--crash-dumps-dir={folder/"crashes"}'])
                    page=b.new_page(viewport={'width':1480,'height':1080})
                    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    page.on('dialog',lambda dialog:dialog.accept())
                    page.goto(f'http://127.0.0.1:{httpd.port}/#/new')
                    expect(page.locator('#dryRun')).not_to_be_checked()
                    expect(page.locator('#maxN')).to_have_value('3')
                    page.locator('#btnPaste').click()
                    page.locator('#batchQty').fill('5000')
                    page.locator('#pasteText').fill('LPC2387FBD 100K\nLPC1788FBD 208K\nLPC2366FBD 100K')
                    page.locator('#btnParse').click()
                    page.locator('#btnStart').click()
                    expect(page.get_by_text('执行批次',exact=True)).to_be_visible()
                    expect(page.get_by_text('任务 #1、#2、#3 · 已排队',exact=True)).to_be_visible()
                    assert len(jobs.recent())==1
                    with db.connect() as c:
                        rows=c.execute('SELECT part_no,qty FROM task ORDER BY id').fetchall()
                        assert [(r['part_no'],r['qty']) for r in rows]==[(p,5000) for p in ('LPC2387FBD100K','LPC1788FBD208K','LPC2366FBD100K')]
                    for index,row in enumerate(rows):
                        with db.connect() as c:tid=c.execute('SELECT id FROM task WHERE part_no=?',(row['part_no'],)).fetchone()[0]
                        sid=db.add_supplier(tid,{'company':f'模拟供应商{index}','qqs':[{'uin':str(123450+index)}]})
                        db.update_supplier(sid,sent_at=time.time(),chat_name=f'模拟会话{index}',status='sent')
                        db.add_message(sid,'out',f"{row['part_no']} 5000pcs")
                    page.goto(f'http://127.0.0.1:{httpd.port}/#/tasks')
                    expect(page.locator('#collectReplies')).to_be_visible()
                    expect(page.locator('#pauseCollectReplies')).to_be_visible()
                    expect(page.locator('#pauseCollectReplies')).to_be_disabled()
                    page.locator('#collectReplies').click()
                    expect(page.locator('#pauseCollectReplies')).to_be_enabled()
                    expect(page.locator('#collectReplies')).to_be_disabled()
                    page.locator('#pauseCollectReplies').click()
                    expect(page.locator('#collectReplies')).to_be_enabled()
                    assert collection.status()['phase']=='paused'
                    page.screenshot(path=str(output/'normal-queue.png'),full_page=True)
                    page.goto(f'http://127.0.0.1:{httpd.port}/#/')
                    page.locator('[data-cancel-job]').click()
                    expect(page.get_by_text('任务 #1、#2、#3 · 已停止',exact=False)).to_be_visible()
                    assert jobs.recent()[0]['status']=='stopped'
                    assert not errors,errors
                    b.close()
                (output/'ui-result.json').write_text(json.dumps({'passed':True,'checks':['正常新建批次','默认每型号三家','任务即时展示','采集独立暂停','批次取消','无网页脚本错误'],'actual_qq_sends':0},ensure_ascii=False,indent=2),encoding='utf-8')
                print('Normal inquiry browser smoke passed; actual QQ sends: 0.')
            finally:
                httpd.shutdown();httpd.server_close()


if __name__=='__main__':main()
