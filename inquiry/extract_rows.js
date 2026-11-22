// 在 ic.net.cn 搜索结果页里执行：提取每一行"可见"的数据（页面混入了很多隐藏的假数据，必须只取可见元素）。
() => {
  const vis = e => e && e.checkVisibility({ visibilityProperty: true, opacityProperty: true });
  const text = e => (e ? e.innerText.replace(/\s+/g, ' ').trim() : '');
  const firstVisible = (row, sel) => Array.from(row.querySelectorAll(sel)).find(vis);
  return Array.from(document.querySelectorAll('#resultList > li')).map((row, index) => {
    const company = firstVisible(row, '.result_supply a.result_goCompany');
    const qqs = Array.from(row.querySelectorAll('.result_askPrice a.qqicon')).filter(vis)
      .map(a => {
        const inthref = a.getAttribute('inthref') || '';
        return {
          uin: inthref.match(/uin=(\d+)/)?.[1] || '',
          title: a.getAttribute('mytitle') || '',
          inthref,
          href: a.getAttribute('href') || '',
        };
      })
      .filter(q => q.uin);
    const level = firstVisible(row, '.result_askPrice .houziLV');
    return {
      index,
      rowClass: row.className,
      company: text(company),
      companyUrl: company ? company.href : '',
      badges: Array.from(row.querySelectorAll('.result_supply .result_icons a, .result_supply .result_icons span')).filter(vis).map(a => a.title || a.className),
      partNo: text(firstVisible(row, '.result_id')),
      mfg: text(firstVisible(row, '.result_factory')),
      batch: text(firstVisible(row, '.result_batchNumber')),
      qty: text(firstVisible(row, '.result_totalNumber')),
      pack: text(firstVisible(row, '.result_pakaging')),
      warehouse: text(firstVisible(row, '.result_kwplace')),
      desc: text(firstVisible(row, '.result_prompt')),
      date: text(firstVisible(row, '.result_date')),
      level: level ? (level.className.match(/LV(\d+)/) || [])[1] || '' : '',
      qqs,
    };
  });
}
