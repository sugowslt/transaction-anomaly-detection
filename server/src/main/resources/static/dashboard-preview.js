(() => {
  const root = document.querySelector('main.layout');
  const header = root.querySelector(':scope > header');
  const cardMetrics = root.querySelector(':scope > .metrics');
  const cardGrid = root.querySelector(':scope > .main-grid');
  const cardTable = root.querySelector(':scope > .panel');
  const bank = root.querySelector(':scope > section[aria-label="전자금융공동망 거래"]');
  const bankMetrics = bank.querySelector(':scope > .metrics');
  const bankGrid = bank.querySelector(':scope > .main-grid');
  const [thresholdTable, historyPanel, monitoringPanel] = bank.querySelectorAll(':scope > article.panel');
  const detectionPanel = cardGrid.children[0];
  const cardForm = cardGrid.children[1];
  const bankTable = bankGrid.children[0];
  const bankForm = bankGrid.children[1];

  header.querySelector('.eyebrow').textContent = '금융거래 이상징후 탐지';
  header.querySelector('h1').textContent = 'Fraud Lab';
  header.querySelector('.lead').textContent = '거래별 판별 결과와 입력값을 확인합니다';

  cardForm.querySelector('h2').textContent = '카드 판별 결과';
  bankForm.querySelector('h2').textContent = '이체 판별 결과';
  cardTable.querySelector('h2').textContent = '거래를 고르세요';
  bankTable.querySelector('h2').textContent = '이체를 고르세요';

  const nav = document.createElement('nav');
  nav.className = 'preview-nav';
  nav.setAttribute('aria-label', '대시보드 메뉴');
  nav.innerHTML = '<button type="button" data-view="overview" aria-current="page">전체 현황</button><button type="button" data-view="workbench">거래 검토</button><button type="button" data-view="history">판별 이력</button><button type="button" data-view="monitoring">모니터링</button><button type="button" data-view="evaluation">모델 평가</button>';
  header.after(nav);

  const section = (key, title, description) => {
    const element = document.createElement('section');
    element.className = 'preview-view';
    element.dataset.view = key;
    element.innerHTML = `<div class="view-heading"><div><h2>${title}</h2><p>${description}</p></div></div>`;
    nav.after(element);
    return element;
  };

  const evaluation = section('evaluation', '모델 평가', '2024년 합성 데이터로 확인한 카드·이체 모델의 검증 결과입니다.');
  const monitoring = section('monitoring', '분포 모니터링', '저장된 이체 판별 기록을 학습 표본의 분포와 비교합니다.');
  const history = section('history', '판별 이력', '서버에 저장된 이체 판별 결과를 확인합니다.');
  const workbench = section('workbench', '거래 검토', '저장된 카드·이체 결과와 입력값을 비교합니다.');

  const kindNav = document.createElement('div');
  kindNav.className = 'kind-nav';
  kindNav.setAttribute('role', 'tablist');
  kindNav.setAttribute('aria-label', '거래 유형');
  kindNav.innerHTML = '<button id="cardTab" type="button" role="tab" data-kind="card" aria-controls="cardWorkspace" aria-selected="true" tabindex="0">카드거래</button><button id="bankTab" type="button" role="tab" data-kind="bank" aria-controls="bankWorkspace" aria-selected="false" tabindex="-1">이체거래</button>';
  workbench.append(kindNav);

  const cardWorkspace = document.createElement('div');
  cardWorkspace.id = 'cardWorkspace';
  cardWorkspace.className = 'workspace-grid';
  cardWorkspace.dataset.kind = 'card';
  cardWorkspace.setAttribute('role', 'tabpanel');
  cardWorkspace.setAttribute('aria-labelledby', 'cardTab');
  cardWorkspace.append(cardTable, cardForm);
  workbench.append(cardWorkspace);

  const bankWorkspace = document.createElement('div');
  bankWorkspace.id = 'bankWorkspace';
  bankWorkspace.className = 'workspace-grid';
  bankWorkspace.dataset.kind = 'bank';
  bankWorkspace.setAttribute('role', 'tabpanel');
  bankWorkspace.setAttribute('aria-labelledby', 'bankTab');
  bankWorkspace.hidden = true;
  bankWorkspace.append(bankTable, bankForm);
  workbench.append(bankWorkspace);

  for (const [resultId, messageId] of [['scoreValue', 'scoreMessage'], ['bankScoreValue', 'bankScoreMessage']]) {
    const value = document.getElementById(resultId);
    const message = document.getElementById(messageId);
    const result = value.closest('.score-result');
    const dial = document.createElement('div');
    dial.className = 'score-dial';
    const copy = document.createElement('div');
    copy.className = 'score-copy';
    const label = document.createElement('small');
    label.textContent = '모델 점수';
    dial.append(value);
    copy.append(label, message);
    const context = document.createElement('div');
    context.id = resultId === 'scoreValue' ? 'cardScoreContext' : 'bankScoreContext';
    context.className = 'score-context';
    copy.append(context);
    result.append(dial, copy);
    const updateScore = () => {
      const score = Number.parseFloat(value.textContent.replace(',', '.'));
      result.style.setProperty('--score-progress', `${Number.isFinite(score) ? Math.min(100, Math.max(0, score)) : 0}%`);
      dial.classList.toggle('precise', value.textContent.length > 6);
      result.classList.toggle('alert-result', /^(?:기존 모델 판정|저장된 모델 판정|모델 판정): 경보(?:$| ·)/.test(message.textContent));
    };
    new MutationObserver(updateScore).observe(value, { childList: true });
    new MutationObserver(updateScore).observe(message, { childList: true });
    updateScore();
  }
  for (const form of [cardForm, bankForm]) {
    const description = form.querySelector('p[id$="SelectedDescription"], #selectedDescription');
    description.after(form.querySelector('.score-result'));
  }

  const fieldLabels = {
    '통합승인금액': '승인금액', '카드이용한도금액': '카드 이용 한도',
    '승인시간대': '승인시간대', '경과일수_최종이용일자': '마지막 이용 후 경과일',
    '전월_매출건수': '전월 매출건수', '전월_매출금액': '전월 매출금액',
    '거래금액': '거래금액', '거래시간대': '거래시간대',
    '자금구분': '자금 종류', '매체구분': '이체 방식',
    '가맹점누적매출금액_구간화': '가맹점 누적매출 구간', '연령': '연령 분류',
    '국내해외여부': '국내·해외 구분', '개인법인구분코드_회원': '회원 구분',
    '승인거래코드': '거래 종류', '승인발생경로코드': '승인 경로',
    '가맹점여부_신규': '신규 가맹점 여부', '인터넷판매여부': '온라인 판매 여부',
    '가맹점상태코드': '가맹점 상태', '가맹점형태구분코드': '가맹점 형태',
    '일시불할부구분코드': '결제 방식', '카드구분코드': '카드 종류',
    '가맹점광역시도코드': '가맹점 지역'
  };
  const moneyFields = new Set(['통합승인금액', '카드이용한도금액', '전월_매출금액', '거래금액']);
  // AI Hub 데이터셋 71925 구축활용가이드 v1.4의 카드거래 코드표.
  const cardCodeLabels = {
    '가맹점누적매출금액_구간화': {
      '0': '0원', '1': '0원 초과~8천 원 미만', '2': '8천 원 이상~100만 원 미만',
      '3': '100만 원 이상~1억 8천만 원 미만', '4': '1억 8천만 원 이상'
    },
    '연령': {
      '1': '19세 이하', '2': '20~29세', '3': '30~39세', '4': '40~49세',
      '5': '50~59세', '6': '60~69세', '7': '70세 이상', '9': '법인'
    },
    '국내해외여부': { '0': '국내', '1': '해외' },
    '개인법인구분코드_회원': { '1': '개인', '2': '법인', '3': '전체', '-': '미등록' },
    '승인거래코드': {
      '00': '국내 일시불', '01': '국내 할부', '10': '체크계좌 승인',
      '11': '체크신용 승인', '60': '해외 구매', '61': '해외 C/A',
      '62': '해외 ATM', '63': '해외 창구 CA', '64': 'Quasi Cash',
      '70': '해외 체크 즉시 출금', '71': '해외 체크 계좌 홀딩',
      '80': '미등록', '_': '미등록'
    },
    '승인발생경로코드': {
      '1': 'ARS', '2': '인터넷', '9': 'BATCH', 'A': '창구',
      'C': '신용카드 승인망', 'O': '해외', 'P': '해외(BC)', 'Q': '국내(BC)', '_': '미등록'
    },
    '가맹점여부_신규': { '0': '신규 가맹점 아님', '1': '신규 가맹점' },
    '인터넷판매여부': { '0': '온라인 판매 아님', '1': '온라인 판매' },
    '가맹점상태코드': { '2': '등록 중', '4': '미등록', '30': '정상', '60': '해지' },
    '가맹점형태구분코드': { '00': '일반 신용 판매', '05': '수기 특약 가맹점', '_': '미등록' },
    '일시불할부구분코드': { 'A': '일시불', 'B': '할부', '_': '미등록' },
    '카드구분코드': {
      '1': '본인', '2': '가족', '3': '계좌 지정(개인형)', '4': '공용', '5': '사용자 지정'
    },
    '가맹점광역시도코드': {
      '01': '강원', '02': '경기', '03': '경남', '04': '경북', '05': '광주',
      '06': '대구', '07': '대전', '08': '부산', '09': '서울', '10': '울산',
      '11': '인천', '12': '전남', '13': '전북', '14': '제주', '15': '충남',
      '16': '충북', '17': '세종'
    }
  };
  const formatField = (key, raw) => {
    const value = String(raw);
    if (moneyFields.has(key) && Number.isFinite(Number(value))) return `${number.format(Number(value))}원`;
    if (key === '거래시간대') return bankTimeLabel(value);
    if (key === '승인시간대') return `${value}시`;
    if (key === '자금구분' || key === '매체구분') return bankCodeLabel(key, value, true);
    if (key === '경과일수_최종이용일자') return `${value}일`;
    if (key === '전월_매출건수') return `${value}건`;
    if (key === '할부가능개월수') return `${value}개월`;
    const codeLabels = cardCodeLabels[key];
    if (codeLabels) return `${codeLabels[value] ?? '코드 설명 없음'} (코드 ${value})`;
    return value;
  };
  const fillFacts = (target, transaction, keys) => {
    target.replaceChildren();
    for (const key of keys) {
      if (!(key in transaction)) continue;
      const group = document.createElement('div');
      const label = document.createElement('dt');
      const value = document.createElement('dd');
      label.textContent = fieldLabels[key] || key.replaceAll('_', ' ');
      value.textContent = formatField(key, transaction[key]);
      group.append(label, value);
      target.append(group);
    }
  };
  const cardHighlights = ['통합승인금액', '승인시간대', '카드이용한도금액', '경과일수_최종이용일자'];
  const bankComparison = cardForm.querySelector('.card-comparison').cloneNode(true);
  bankComparison.querySelectorAll('[id]').forEach(element => { element.id = element.id.replace(/^card/, 'bank'); });
  bankComparison.setAttribute('aria-labelledby', 'bankComparisonTitle');
  bankComparison.querySelector('label').htmlFor = 'bankCompareSelect';
  bankForm.append(bankComparison);
  bankTable.querySelector('h2').insertAdjacentHTML('afterend', '<div class="card-data-status"><span id="bankRefreshStatus" role="status">저장된 이체 결과</span><button type="button" id="bankRefreshButton">결과 새로고침</button></div>');
  document.getElementById('bankRefreshButton').addEventListener('click', () => loadBankResults());
  for (const [kind, getExamples, getSelected, getThreshold] of [
    ['card', () => examples, () => selected, () => cardThreshold],
    ['bank', () => bankExamples, () => bankSelected, () => bankThreshold]
  ]) {
    const comparisonSelect = document.getElementById(`${kind}CompareSelect`);
  const renderComparison = () => {
    const selected = getSelected();
    const compared = getExamples().find(item => item.id === comparisonSelect.value && item.id !== selected?.id);
    const table = document.getElementById(`${kind}CompareTable`);
    const rows = document.getElementById(`${kind}CompareRows`);
    const summary = document.getElementById(`${kind}CompareSummary`);
    rows.replaceChildren();
    table.hidden = !selected || !compared;
    if (!selected || !compared) {
      summary.textContent = selected ? '비교할 거래를 고르면 서로 다른 입력 항목을 보여드립니다.' : '저장된 거래가 없습니다.';
      return;
    }
    document.getElementById(`${kind}CompareLeft`).textContent = selected.id;
    document.getElementById(`${kind}CompareRight`).textContent = compared.id;
    const keys = [...new Set([...Object.keys(selected.transaction), ...Object.keys(compared.transaction)])];
    const differences = keys.filter(key => String(selected.transaction[key]) !== String(compared.transaction[key]));
    const appendRow = (label, left, right) => {
      const row = document.createElement('tr');
      for (const text of [label, left, right]) {
        const cell = document.createElement('td');
        cell.textContent = text;
        row.append(cell);
      }
      rows.append(row);
    };
    appendRow('모델 점수', scorePercent(selected.riskScore), scorePercent(compared.riskScore));
    appendRow('판별 결과', selected.alert ? '경보' : '경보 없음', compared.alert ? '경보' : '경보 없음');
    for (const key of differences) appendRow(fieldLabels[key] || key,
      key in selected.transaction ? formatField(key, selected.transaction[key]) : '입력 없음',
      key in compared.transaction ? formatField(key, compared.transaction[key]) : '입력 없음');
    summary.textContent = `${keys.length}개 입력 중 ${differences.length}개가 다릅니다. 두 거래의 경보 기준은 ${scorePercent(getThreshold())}입니다.`;
  };
  const updateComparisonOptions = () => {
    const selected = getSelected();
    const previous = comparisonSelect.value;
    comparisonSelect.replaceChildren(new Option('비교할 거래 선택', ''));
    for (const item of getExamples().filter(item => item.id !== selected?.id)) {
      comparisonSelect.add(new Option(`${item.id} · ${item.scenario}`, item.id));
    }
    comparisonSelect.value = getExamples().some(item => item.id === previous && item.id !== selected?.id) ? previous : '';
    comparisonSelect.disabled = !selected || getExamples().length < 2;
    renderComparison();
  };
  comparisonSelect.addEventListener('change', renderComparison);
  new MutationObserver(updateComparisonOptions).observe(document.getElementById(kind === 'card' ? 'selectedDescription' : 'bankSelectedDescription'), { childList: true });
  updateComparisonOptions();
  }
  for (const [form, kind, getItem] of [[cardForm, 'card', () => selected], [bankForm, 'bank', () => bankSelected]]) {
    const detail = document.createElement('section');
    detail.className = 'case-detail';
    detail.setAttribute('aria-label', '선택한 가상 거래의 세부정보');
    detail.innerHTML = '<div class="case-detail-head"><h3>사례 세부정보</h3><span class="case-detail-id"></span></div><p class="case-detail-note">선택한 가상 거래의 원본 입력입니다. 수정한 값은 위 입력칸에서 확인하세요.</p><dl class="case-facts"></dl>';
    detail.querySelector('.case-detail-note').textContent = '저장된 판별 결과에 사용된 입력값입니다.';
    if (kind === 'card') {
      const allFields = document.createElement('details');
      allFields.className = 'case-all-fields';
      allFields.innerHTML = '<summary>모델 입력 전체 보기 <span class="field-count"></span></summary><dl class="case-facts case-facts-all"></dl>';
      detail.append(allFields);
    }
    const codeNote = document.createElement('p');
    codeNote.className = 'case-code-note';
    codeNote.textContent = kind === 'card'
      ? '카드 코드의 이름은 AI Hub 구축활용가이드 v1.4를 따릅니다. 괄호 안은 모델에 전달되는 원래 코드입니다.'
      : '자금 종류와 이체 방식은 확인된 코드표의 이름과 원래 값을 함께 표시합니다.';
    detail.append(codeNote);
    form.append(detail);
    form.classList.add('review-panel');
    const comparison = form.querySelector('.card-comparison');
    const detailTabs = document.createElement('div');
    detailTabs.className = 'review-tabs';
    detailTabs.setAttribute('role', 'tablist');
    detailTabs.setAttribute('aria-label', '거래 검토 방식');
    detailTabs.innerHTML = `<button id="${kind}DetailTab" type="button" role="tab" aria-controls="${kind}DetailPanel" aria-selected="true">입력 상세</button><button id="${kind}CompareTab" type="button" role="tab" aria-controls="${kind}ComparePanel" aria-selected="false" tabindex="-1">거래 비교</button>`;
    detail.id = `${kind}DetailPanel`; comparison.id = `${kind}ComparePanel`;
    for (const [panel, label] of [[detail, `${kind}DetailTab`], [comparison, `${kind}CompareTab`]]) {
      panel.setAttribute('role', 'tabpanel'); panel.setAttribute('aria-labelledby', label);
    }
    comparison.before(detailTabs);
    comparison.hidden = true;
    const choose = index => {
      const buttons = [...detailTabs.children];
      buttons.forEach((button, i) => { button.setAttribute('aria-selected', String(i === index)); button.tabIndex = i === index ? 0 : -1; });
      detail.hidden = index !== 0 || !getItem(); comparison.hidden = index !== 1;
    };
    [...detailTabs.children].forEach((button, index) => button.addEventListener('click', () => choose(index)));
    detailTabs.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const index = event.key === 'Home' ? 0 : event.key === 'End' ? 1 : detailTabs.children[0].getAttribute('aria-selected') === 'true' ? 1 : 0;
      choose(index); detailTabs.children[index].focus();
    });
    const render = () => {
      const item = getItem();
      detail.hidden = !item || detailTabs.children[0].getAttribute('aria-selected') !== 'true';
      if (!item) return;
      detail.querySelector('.case-detail-id').textContent = `${item.id} · ${item.scenario}`;
      fillFacts(detail.querySelector('.case-facts'), item.transaction, kind === 'card' ? cardHighlights : Object.keys(item.transaction));
      if (kind === 'card') {
        const keys = Object.keys(item.transaction);
        detail.querySelector('.field-count').textContent = `${keys.length}개 항목`;
        fillFacts(detail.querySelector('.case-facts-all'), item.transaction, keys);
      }
    };
    new MutationObserver(render).observe(form.querySelector('p[id$="SelectedDescription"], #selectedDescription'), { childList: true });
    render();
  }

  history.append(historyPanel);
  monitoring.append(monitoringPanel);
  const detailOverlay = document.createElement('div');
  detailOverlay.className = 'detail-overlay';
  detailOverlay.hidden = true;
  detailOverlay.innerHTML = `
    <div class="detail-backdrop" aria-hidden="true"></div>
    <aside class="detail-sheet" role="dialog" aria-modal="true" aria-labelledby="decisionDetailTitle">
      <div class="detail-sheet-head"><h2 id="decisionDetailTitle">이체 판별 상세</h2><button type="button" class="detail-close" aria-label="상세 닫기">닫기</button></div>
      <p class="detail-date"></p>
      <div class="detail-verdict"><span class="detail-verdict-label"></span><strong class="detail-score"></strong></div>
      <div class="detail-meter" role="img" aria-label="모델 점수와 저장된 경보 기준 비교"><span class="detail-meter-fill"></span><i class="detail-meter-threshold"></i></div>
      <div class="detail-meter-values"><span>모델 점수 <b class="detail-score-text"></b></span><span>경보 기준 <b class="detail-threshold-text"></b></span></div>
      <h3>저장된 거래 정보</h3><dl class="detail-facts"></dl>
      <p class="detail-disclaimer">자금 종류와 이체 방식은 코드표에 따라 표시합니다. 계좌와 금융회사 식별자는 저장하지 않습니다.</p>
    </aside>`;
  document.body.append(detailOverlay);
  const historyBody = document.getElementById('bankDecisions');
  let selectedHistoryRow = null;
  let previousFocus = null;
  const closeDecisionDetail = () => {
    if (detailOverlay.hidden) return;
    detailOverlay.hidden = true;
    document.body.classList.remove('detail-open');
    root.inert = false;
    selectedHistoryRow?.classList.remove('detail-selected');
    selectedHistoryRow = null;
    if (previousFocus?.isConnected) previousFocus.focus();
    previousFocus = null;
  };
  const openDecisionDetail = row => {
    const item = bankDecisionItems.find(entry => entry.id === row.dataset.decisionId);
    if (!item) return;
    previousFocus = row;
    selectedHistoryRow?.classList.remove('detail-selected');
    selectedHistoryRow = row;
    row.classList.add('detail-selected');
    detailOverlay.querySelector('.detail-date').textContent = new Date(item.createdAt).toLocaleString('ko-KR');
    const verdict = detailOverlay.querySelector('.detail-verdict');
    verdict.classList.toggle('is-alert', item.alert);
    detailOverlay.classList.toggle('is-alert', item.alert);
    detailOverlay.querySelector('.detail-verdict-label').textContent = item.alert ? '경보로 판별했습니다' : '경보 기준 미만입니다';
    detailOverlay.querySelector('.detail-score').textContent = percent(item.riskScore);
    detailOverlay.querySelector('.detail-score-text').textContent = percent(item.riskScore);
    detailOverlay.querySelector('.detail-threshold-text').textContent = percent(item.threshold);
    detailOverlay.querySelector('.detail-meter-fill').style.width = `${Math.min(100, Math.max(0, item.riskScore * 100))}%`;
    detailOverlay.querySelector('.detail-meter-threshold').style.left = `${Math.min(100, Math.max(0, item.threshold * 100))}%`;
    const facts = detailOverlay.querySelector('.detail-facts');
    facts.replaceChildren();
    for (const [labelText, valueText] of [
      ['거래금액', `${number.format(item.amount)}원`], ['거래시간대', bankTimeLabel(item.timeBucket)],
      ['자금 종류', bankCodeLabel('자금구분', item.fundType, true)],
      ['이체 방식', bankCodeLabel('매체구분', item.channel, true)]
    ]) {
      const group = document.createElement('div');
      const label = document.createElement('dt');
      const value = document.createElement('dd');
      label.textContent = labelText;
      value.textContent = String(valueText);
      group.append(label, value);
      facts.append(group);
    }
    detailOverlay.hidden = false;
    document.body.classList.add('detail-open');
    root.inert = true;
    detailOverlay.querySelector('.detail-close').focus();
  };
  const enableHistoryRows = () => historyBody.querySelectorAll('tr[data-decision-id]').forEach(row => {
    if (row.hasAttribute('tabindex')) return;
    row.tabIndex = 0;
    row.setAttribute('role', 'button');
    row.setAttribute('aria-haspopup', 'dialog');
    row.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openDecisionDetail(row); }
    });
  });
  new MutationObserver(enableHistoryRows).observe(historyBody, { childList: true });
  enableHistoryRows();
  historyBody.addEventListener('click', event => {
    const row = event.target.closest('tr[data-decision-id]');
    if (row) openDecisionDetail(row);
  });
  detailOverlay.querySelector('.detail-close').addEventListener('click', closeDecisionDetail);
  detailOverlay.querySelector('.detail-backdrop').addEventListener('click', closeDecisionDetail);
  detailOverlay.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); closeDecisionDetail(); }
    if (event.key === 'Tab') { event.preventDefault(); detailOverlay.querySelector('.detail-close').focus(); }
  });
  const evaluationNav = document.createElement('div');
  evaluationNav.className = 'kind-nav evaluation-nav';
  evaluationNav.setAttribute('role', 'tablist');
  evaluationNav.setAttribute('aria-label', '평가할 거래 유형');
  evaluationNav.innerHTML = '<button id="evaluationCardTab" type="button" role="tab" data-evaluation-kind="card" aria-controls="evaluationCardPanel" aria-selected="true" tabindex="0">카드거래</button><button id="evaluationBankTab" type="button" role="tab" data-evaluation-kind="bank" aria-controls="evaluationBankPanel" aria-selected="false" tabindex="-1">이체거래</button>';
  evaluation.append(evaluationNav);
  const evaluationGroup = document.createElement('div');
  evaluationGroup.id = 'evaluationCardPanel';
  evaluationGroup.className = 'evaluation-group';
  evaluationGroup.setAttribute('role', 'tabpanel');
  evaluationGroup.setAttribute('aria-labelledby', 'evaluationCardTab');
  evaluationGroup.append(cardMetrics, detectionPanel);
  evaluation.append(evaluationGroup);
  const bankEvaluation = document.createElement('div');
  bankEvaluation.id = 'evaluationBankPanel';
  bankEvaluation.className = 'evaluation-group';
  bankEvaluation.setAttribute('role', 'tabpanel');
  bankEvaluation.setAttribute('aria-labelledby', 'evaluationBankTab');
  bankEvaluation.hidden = true;
  bankEvaluation.append(bankMetrics, thresholdTable);
  evaluation.append(bankEvaluation);
  cardGrid.remove();
  bank.remove();

  const overview = section('overview', '전체 현황', '로컬 판별 기록, 데이터 변화, 모델 검증 결과를 함께 확인합니다.');
  overview.innerHTML += `
    <div class="overview-toolbar"><span id="overviewUpdated" role="status">불러오는 중</span><button type="button" id="overviewRefresh">현황 새로고침</button></div>
    <div class="overview-kpis">
      <div><span>최근 이체 기록</span><strong id="overviewCount">—</strong><small>저장된 최근 최대 1,000건</small></div>
      <div><span>경보 기록</span><strong id="overviewAlerts">—</strong><small id="overviewAlertRate">집계 대기</small></div>
      <div><span>관찰할 입력 항목</span><strong id="overviewDriftCount">—</strong><small>금액·시간대·자금·매체</small></div>
      <div><span>판별 서비스</span><strong id="overviewService">—</strong><small id="overviewServiceNote">연결 확인 중</small></div>
    </div>
    <div class="overview-grid">
      <article class="panel overview-recent"><div class="section-head"><h2>최근 판별 이력</h2><button type="button" data-open-view="history">전체 이력</button></div><div class="table-wrap"><table><thead><tr><th>판별 시각</th><th>금액</th><th>모델 점수</th><th>판정</th></tr></thead><tbody id="overviewRecent"><tr><td colspan="4">불러오는 중</td></tr></tbody></table></div><p>시연 거래가 포함된 로컬 저장 기록입니다.</p></article>
      <article class="panel"><div class="section-head"><h2>모델 검증 성능</h2><button type="button" data-open-view="evaluation">상세 평가</button></div><div class="table-wrap"><table><thead><tr><th>거래</th><th>정밀도</th><th>재현율</th><th>검증 건수</th></tr></thead><tbody id="overviewEvaluation"></tbody></table></div><p>2024년 합성 데이터 기준. 최근 경보 비율과 구분합니다.</p></article>
      <article class="panel overview-distribution"><div class="section-head"><h2>최근 입력 분포</h2><button type="button" data-open-view="monitoring">상세 분포</button></div><div id="overviewDrift" class="overview-drift">불러오는 중</div><p>학습 표본과 비교한 참고 신호입니다. 경보 정확도를 뜻하지 않습니다.</p></article>
    </div>
    <details class="dashboard-source"><summary>데이터 출처와 시연 범위</summary><p>카드·이체 모델은 AI Hub 금융거래 합성데이터로 학습했습니다. 거래 검토는 서버의 시연 결과 파일을 읽고, 판별 이력과 분포는 로컬 DB의 이체 기록을 읽습니다.</p></details>`;
  const sourceFooter = root.querySelector('footer');
  if (sourceFooter) overview.querySelector('.dashboard-source').append(sourceFooter);
  for (const [targetId, title] of [['referenceVersions', '학습 기준 이력'], ['monitorTimeline', '활동일별 판별 추이']]) {
    const target = document.getElementById(targetId);
    const content = targetId === 'referenceVersions' ? target.closest('.table-wrap') : target;
    const note = targetId === 'referenceVersions' ? content.previousElementSibling : null;
    const heading = note ? note.previousElementSibling : content.previousElementSibling;
    const disclosure = document.createElement('details'); disclosure.className = 'monitor-disclosure';
    const summary = document.createElement('summary'); summary.textContent = title; disclosure.append(summary);
    heading.replaceWith(disclosure);
    if (note) disclosure.append(note);
    disclosure.append(content);
  }
  const demoAction = monitoringPanel.querySelector('.monitor-demo');
  demoAction.classList.add('overview-action');
  overview.querySelector('.dashboard-source').before(demoAction);
  document.getElementById('seedMonitoringButton').textContent = '가상 이체 32건 판별·기록';
  overview.addEventListener('click', event => {
    const target = event.target.closest('[data-open-view]');
    if (target) showView(target.dataset.openView);
  });
  let overviewLoading = false;
  let overviewRefreshPending = false;
  const refreshOverview = async () => {
    if (overviewLoading) { overviewRefreshPending = true; return; }
    overviewLoading = true;
    const button = document.getElementById('overviewRefresh');
    button.disabled = true; button.setAttribute('aria-busy', 'true');
    document.getElementById('overviewUpdated').textContent = '서버에서 현황을 불러오는 중입니다.';
    const urls = ['/api/bank/monitoring', '/api/bank/decisions?limit=5', '/api/metrics', '/api/bank/metrics', '/api/model-health'];
    const results = await Promise.allSettled(urls.map(getJson));
    const [monitor, recent, cardReport, bankReport, health] = results.map(result => result.status === 'fulfilled' ? result.value : null);
    const put = (id, text) => { document.getElementById(id).textContent = text; };
    if (monitor) {
      const alerts = monitor.modelVersions.reduce((sum, item) => sum + item.alerts, 0);
      put('overviewCount', `${number.format(monitor.sampleSize)}건`);
      put('overviewAlerts', `${number.format(alerts)}건`);
      put('overviewAlertRate', monitor.sampleSize ? `최근 경보 비율 ${percent(alerts / monitor.sampleSize)}` : '저장된 기록 없음');
      put('overviewDriftCount', monitor.ready ? `${monitor.drift.filter(item => ['WATCH', 'DRIFT'].includes(item.status)).length} / ${monitor.drift.length}` : '표본 대기');
      const labels = { STABLE: '안정', WATCH: '관찰', DRIFT: '변화 큼', INSUFFICIENT_DATA: '표본 대기' };
      document.getElementById('overviewDrift').innerHTML = monitor.drift.map(item => `<div><span>${escapeHtml(fieldLabels[item.feature] || item.feature)}</span><strong>${escapeHtml(labels[item.status] || '확인 필요')}</strong></div>`).join('');
    } else {
      for (const id of ['overviewCount', 'overviewAlerts', 'overviewDriftCount']) put(id, '—');
      put('overviewAlertRate', '집계 불러오기 실패'); put('overviewDrift', '분포를 불러오지 못했습니다. 새로고침해 주세요.');
    }
    document.getElementById('overviewRecent').innerHTML = recent ? recent.items.map(item => `<tr><td>${escapeHtml(new Date(item.createdAt).toLocaleString('ko-KR'))}</td><td>${number.format(item.amount)}원</td><td>${scorePercent(item.riskScore)}</td><td><span class="chip ${item.alert ? 'tp' : 'tn'}">${item.alert ? '경보' : '경보 없음'}</span></td></tr>`).join('') || '<tr><td colspan="4">저장된 기록이 없습니다.</td></tr>' : '<tr><td colspan="4">판별 이력을 불러오지 못했습니다.</td></tr>';
    document.getElementById('overviewEvaluation').innerHTML = [['카드', cardReport], ['이체', bankReport]].map(([label, report]) => report ? `<tr><td>${label}</td><td>${percent(report.validation.precision)}</td><td>${percent(report.validation.recall)}</td><td>${number.format(report.validation.rows)}</td></tr>` : `<tr><td>${label}</td><td colspan="3">검증 결과 불러오기 실패</td></tr>`).join('');
    const matched = health?.status === 'ok' && cardReport && bankReport && health.models?.card === cardReport.model_version && health.models?.bank === bankReport.model_version;
    put('overviewService', matched ? '연결됨' : '확인 필요');
    put('overviewServiceNote', matched ? '카드·이체 모델 일치' : '연결 또는 모델 결과 확인 필요');
    const failures = results.filter(result => result.status === 'rejected').length;
    put('overviewUpdated', `${new Date().toLocaleTimeString('ko-KR')} 확인${failures ? ` · ${failures}개 항목 불러오기 실패` : ''}`);
    button.disabled = false; button.removeAttribute('aria-busy'); overviewLoading = false;
    if (overviewRefreshPending) { overviewRefreshPending = false; refreshOverview(); }
  };
  document.getElementById('overviewRefresh').addEventListener('click', refreshOverview);
  document.addEventListener('dashboard:data-changed', refreshOverview);
  refreshOverview();

  const showView = view => {
    root.querySelectorAll('.preview-view').forEach(element => { element.hidden = element.dataset.view !== view; });
    nav.querySelectorAll('button').forEach(button => {
      if (button.dataset.view === view) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
    window.history.replaceState(null, '', `#${view}`);
  };
  nav.addEventListener('click', event => {
    const button = event.target.closest('button[data-view]');
    if (button) showView(button.dataset.view);
  });
  showView(['overview', 'workbench', 'history', 'monitoring', 'evaluation'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'overview');

  const showKind = kind => {
    workbench.querySelectorAll('.workspace-grid').forEach(element => { element.hidden = element.dataset.kind !== kind; });
    kindNav.querySelectorAll('button').forEach(button => {
      const active = button.dataset.kind === kind;
      button.setAttribute('aria-selected', String(active));
      button.tabIndex = active ? 0 : -1;
    });
  };
  kindNav.addEventListener('click', event => {
    const button = event.target.closest('button[data-kind]');
    if (button) showKind(button.dataset.kind);
  });
  kindNav.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const current = kindNav.querySelector('[aria-selected="true"]');
    const nextKind = event.key === 'Home' ? 'card' : event.key === 'End' ? 'bank' : current.dataset.kind === 'card' ? 'bank' : 'card';
    showKind(nextKind);
    kindNav.querySelector(`[data-kind="${nextKind}"]`).focus();
  });

  const showEvaluationKind = kind => {
    evaluationGroup.hidden = kind !== 'card';
    bankEvaluation.hidden = kind !== 'bank';
    evaluationNav.querySelectorAll('button').forEach(button => {
      const active = button.dataset.evaluationKind === kind;
      button.setAttribute('aria-selected', String(active));
      button.tabIndex = active ? 0 : -1;
    });
  };
  evaluationNav.addEventListener('click', event => {
    const button = event.target.closest('button[data-evaluation-kind]');
    if (button) showEvaluationKind(button.dataset.evaluationKind);
  });
  evaluationNav.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const current = evaluationNav.querySelector('[aria-selected="true"]');
    const nextKind = event.key === 'Home' ? 'card' : event.key === 'End' ? 'bank' : current.dataset.evaluationKind === 'card' ? 'bank' : 'card';
    showEvaluationKind(nextKind);
    evaluationNav.querySelector(`[data-evaluation-kind="${nextKind}"]`).focus();
  });

  for (const id of ['transactions', 'bankTransactions']) {
    const body = document.getElementById(id);
    const enableKeyboard = () => body.querySelectorAll('tr[data-id]').forEach(row => {
      if (row.hasAttribute('tabindex')) return;
      row.tabIndex = 0;
      row.setAttribute('role', 'button');
      row.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); row.click(); }
      });
    });
    new MutationObserver(enableKeyboard).observe(body, { childList: true });
    enableKeyboard();
  }
})();
