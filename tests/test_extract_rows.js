const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const test = require('node:test');

const script = fs.readFileSync(path.join(__dirname, '..', 'inquiry', 'extract_rows.js'), 'utf8');
const extract = new Function('document', `return (${script})();`);

function icon(attrs, visible = true) {
  return {
    checkVisibility(options) {
      assert.deepEqual(options, { visibilityProperty: true, opacityProperty: true });
      return visible;
    },
    getAttribute(name) {
      return attrs[name] ?? null;
    },
    get href() {
      throw new Error('应读取原始 href 属性');
    },
  };
}

function extractIcons(icons) {
  const row = {
    className: 'fixture',
    querySelectorAll(selector) {
      return selector === '.result_askPrice a.qqicon' ? icons : [];
    },
  };
  return extract({
    querySelectorAll(selector) {
      assert.equal(selector, '#resultList > li');
      return [row];
    },
  })[0].qqs;
}

test('保留原始链接、参数顺序和相对 href，缺少属性时使用空字符串', () => {
  const inthref = '  https://example.invalid/qq?uin=0012345&key=A%2bb#part  ';
  const href = '../qq/open?name=%E6%B5%8B%E8%AF%95&t=plus+sign';
  const secondInthref = '/qq?x=1&uin=12346&x=2';
  assert.deepEqual(extractIcons([
    icon({ inthref, href, mytitle: '测试 对话' }),
    icon({ inthref: secondInthref }),
  ]), [
    { uin: '0012345', title: '测试 对话', inthref, href },
    { uin: '12346', title: '', inthref: secondInthref, href: '' },
  ]);
});

test('忽略隐藏图标、无数字 uin 的图标和缺少 inthref 的旧图标', () => {
  const inthref = '/qq?uin=12347&kind=fixture';
  assert.deepEqual(extractIcons([
    icon({ inthref: '/qq?uin=98765' }, false),
    icon({ inthref: '/qq?uin=abc', href: '/qq?uin=23456' }),
    icon({ href: '/qq?uin=23456' }),
    icon({ inthref }),
  ]), [
    { uin: '12347', title: '', inthref, href: '' },
  ]);
});
