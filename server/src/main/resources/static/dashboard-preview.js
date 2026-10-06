(() => {
  const root = document.querySelector('main.layout');
  const contextualKind = item => item.modelVersion?.startsWith('bank-context-v2-candidate-') ? 'v2'
    : item.modelVersion?.startsWith('bank-context-v1-') ? 'v1' : null;
  const contextualPath = kind => kind === 'v2' ? '/api/bank/contextual-v2' : '/api/bank/contextual';
  const evidenceCache = new Map();
  const readContextualEvidence = item => {
    const kind = contextualKind(item);
    if (!evidenceCache.has(item.id)) {
      evidenceCache.set(item.id, getJson(`${contextualPath(kind)}-decisions/${encodeURIComponent(item.id)}`).catch(error => {
        evidenceCache.delete(item.id); throw error;
      }));
    }
    return evidenceCache.get(item.id);
  };
  const supportLabel = evidence => ({
    outside_fit_support: '금액이 학습 범위 밖', insufficient_context: '이전 거래 맥락 부족',
    outside_temporal_validation: '거래일자가 검증 연도 밖', within_observed_support: '합성 데이터 관측 범위 안 · 실거래 보증 아님',
  }[evidence.supportAssessment?.status] || '검증 범위 확인 필요');
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
  bankForm.querySelector('h2').textContent = '저장된 이체 판별 결과';
  cardTable.querySelector('h2').textContent = '거래를 고르세요';
  bankTable.querySelector('h2').textContent = '이체를 고르세요';

  const nav = document.createElement('nav');
  nav.className = 'preview-nav';
  nav.setAttribute('aria-label', '대시보드 메뉴');
  nav.innerHTML = '<button type="button" data-view="overview" aria-current="page">전체 현황</button><button type="button" data-view="workbench">거래 검토</button><button type="button" data-view="history">판별 이력</button><button type="button" data-view="monitoring">모니터링</button>';
  header.after(nav);

  const section = (key, title, description) => {
    const element = document.createElement('section');
    element.className = 'preview-view';
    element.dataset.view = key;
    element.innerHTML = `<div class="view-heading"><div><h2>${title}</h2><p>${description}</p></div></div>`;
    nav.after(element);
    return element;
  };

  const monitoring = section('monitoring', '분포 모니터링', '저장된 이체 판별 기록을 학습 표본의 분포와 비교합니다.');
  const history = section('history', '판별 이력', '서버에 저장된 이체 판별 결과를 확인합니다.');
  const workbench = section('workbench', '거래 검토', '이체 저장 기록과 별도 시연 사례의 판별 결과·입력값을 확인합니다.');

  const kindNav = document.createElement('div');
  kindNav.className = 'kind-nav';
  kindNav.setAttribute('role', 'tablist');
  kindNav.setAttribute('aria-label', '거래 유형');
  kindNav.innerHTML = '<button id="cardTab" type="button" role="tab" data-kind="card" aria-controls="cardWorkspace" aria-selected="false" tabindex="-1">카드거래</button><button id="bankTab" type="button" role="tab" data-kind="bank" aria-controls="bankWorkspace" aria-selected="true" tabindex="0">이체거래</button>';
  workbench.append(kindNav);

  const cardWorkspace = document.createElement('div');
  cardWorkspace.id = 'cardWorkspace';
  cardWorkspace.className = 'workspace-grid';
  cardWorkspace.dataset.kind = 'card';
  cardWorkspace.setAttribute('role', 'tabpanel');
  cardWorkspace.setAttribute('aria-labelledby', 'cardTab');
  cardWorkspace.hidden = true;
  cardWorkspace.append(cardTable, cardForm);
  workbench.append(cardWorkspace);

  const bankWorkspace = document.createElement('div');
  bankWorkspace.id = 'bankWorkspace';
  bankWorkspace.className = 'workspace-grid';
  bankWorkspace.dataset.kind = 'bank';
  bankWorkspace.setAttribute('role', 'tabpanel');
  bankWorkspace.setAttribute('aria-labelledby', 'bankTab');
  bankWorkspace.hidden = false;
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
  bankTable.querySelector('h2').insertAdjacentHTML('afterend', '<div class="review-source" id="bankReviewSources" aria-label="이체 기록 종류"><button type="button" data-review-source="stored" class="active">전체 저장 기록</button><button type="button" data-review-source="demo">기존 모델 시연 사례 8건</button></div><div class="card-data-status"><span id="bankReviewStatus" role="status">저장 기록을 불러오는 중입니다.</span><button type="button" id="bankRefreshButton">새로고침</button></div>');
  bankTable.querySelector('th:nth-child(2)').textContent = '기록 / 사례';
  bankTable.querySelector('.table-wrap').after(Object.assign(document.createElement('button'), { id: 'bankReviewLoadMore', type: 'button', textContent: '이후 기록 더 보기', hidden: true }));
  document.getElementById('bankReviewSources').addEventListener('click', event => {
    const button = event.target.closest('button[data-review-source]');
    if (button) setBankReviewSource(button.dataset.reviewSource);
  });
  document.getElementById('bankReviewLoadMore').addEventListener('click', () => loadBankStoredDecisions(false));
  document.getElementById('bankRefreshButton').addEventListener('click', async () => {
    await Promise.all([loadBankResults(), loadBankStoredDecisions()]);
  });
  loadBankStoredDecisions();
  for (const [kind, getExamples, getSelected, getThreshold] of [
    ['card', () => examples, () => selected, () => cardThreshold],
    ['bank', bankReviewItems, () => bankSelected, () => bankSelected?.threshold ?? bankThreshold]
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
    if (kind === 'bank' && selected.threshold !== compared.threshold) {
      appendRow('경보 기준', scorePercent(selected.threshold ?? bankThreshold), scorePercent(compared.threshold ?? bankThreshold));
    }
    for (const key of differences) appendRow(fieldLabels[key] || key,
      key in selected.transaction ? formatField(key, selected.transaction[key]) : '입력 없음',
      key in compared.transaction ? formatField(key, compared.transaction[key]) : '입력 없음');
    summary.textContent = `${keys.length}개 입력 중 ${differences.length}개가 다릅니다.${kind === 'bank' && selected.threshold !== compared.threshold ? ' 저장 당시 경보 기준은 거래별로 표에 표시합니다.' : ` 경보 기준은 ${scorePercent(getThreshold())}입니다.`}`;
    if (kind === 'bank' && selected.modelVersion !== compared.modelVersion) summary.textContent += ' 서로 다른 모델의 기록이므로 입력 차이만으로 점수 차이의 원인을 판단할 수 없습니다.';
  };
  const updateComparisonOptions = () => {
    const selected = getSelected();
    const previous = comparisonSelect.value;
    comparisonSelect.replaceChildren(new Option('비교할 거래 선택', ''));
    for (const item of getExamples().filter(item => item.id !== selected?.id)) {
      comparisonSelect.add(new Option(`${item.source === 'stored' ? item.id.slice(0, 8) : item.id} · ${item.scenario}`, item.id));
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
    detail.setAttribute('aria-label', '선택한 거래의 세부정보');
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
    if (kind === 'card') detail.querySelector('.case-all-fields').append(codeNote);
    else detail.append(codeNote);
    if (kind === 'bank') {
      const record = document.createElement('details');
      record.className = 'case-record';
      record.innerHTML = '<summary>저장 기록 정보</summary><dl class="case-facts"></dl>';
      detail.append(record);
    }
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
      detail.querySelector('.case-detail-id').textContent = kind === 'bank'
        ? (item.source === 'stored' ? item.id.slice(0, 8) : item.id)
        : `${item.id} · ${item.scenario}`;
      fillFacts(detail.querySelector('.case-facts'), item.transaction, kind === 'card' ? cardHighlights : Object.keys(item.transaction));
      if (kind === 'bank') {
        const record = detail.querySelector('.case-record');
        record.hidden = item.source !== 'stored';
        if (item.source === 'stored') {
          const facts = record.querySelector('.case-facts');
          facts.replaceChildren();
          for (const [label, value] of [['판별 기록 ID', item.id], ['저장 시각', item.recordDate], ['경보 기준', scorePercent(item.threshold)], ['모델 식별값', item.modelVersion]]) {
            const group = document.createElement('div');
            const term = document.createElement('dt'); term.textContent = label;
            const description = document.createElement('dd'); description.textContent = value;
            group.append(term, description); facts.append(group);
          }
          record.querySelector('.inspect-contextual')?.remove();
          const pathKind = contextualKind(item);
          if (pathKind) {
            const inspect = document.createElement('button');
            inspect.type = 'button'; inspect.className = 'inspect-contextual';
            inspect.textContent = '이 거래에 사용된 과거 맥락 보기';
            inspect.addEventListener('click', () => document.dispatchEvent(new CustomEvent('dashboard:inspect-contextual', {detail: {id: item.id, kind: pathKind}})));
            record.append(inspect);
            detail.querySelector('.case-detail-note').textContent = `${pathKind === 'v2' ? '입출금 흐름' : '과거 출금 이력'} 모델의 저장 결과입니다. 아래 거래값과 거래일자·계좌 관계·과거 이력을 함께 사용했습니다.`;
            readContextualEvidence(item).then(evidence => {
              if (getItem()?.id !== item.id) return;
              const limited = evidence.supportAssessment?.status !== 'within_observed_support';
              document.getElementById('bankScoreMessage').textContent = limited
                ? `${item.alert ? '경보 · ' : ''}검증 범위 제한` : item.alert ? '저장된 모델 판정: 경보' : '저장된 모델 판정: 경보 기준 미만';
              const group = document.createElement('div');
              const term = document.createElement('dt'); term.textContent = '검증 범위';
              const description = document.createElement('dd'); description.textContent = supportLabel(evidence);
              group.append(term, description); detail.querySelector('.case-facts').append(group);
            }).catch(() => {
              if (getItem()?.id === item.id) detail.querySelector('.case-detail-note').textContent += ' 저장된 맥락 상세를 불러오지 못했습니다.';
            });
          } else {
            detail.querySelector('.case-detail-note').textContent = '기존 4개 필드 이체 모델에 전달한 입력값입니다.';
          }
        } else {
          record.querySelector('.inspect-contextual')?.remove();
          detail.querySelector('.case-detail-note').textContent = '기존 4개 필드 이체 모델의 가상 시연 입력입니다. 정답 라벨이 있는 평가 거래가 아닙니다.';
        }
      }
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
      <p class="detail-disclaimer">모델 식별값은 이 거래를 판별한 모델 파일을 구분합니다. 계좌·금융회사 식별자는 화면에 공개하지 않습니다.</p>
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
  const openDecisionDetail = async row => {
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
    detailOverlay.querySelector('.inspect-contextual')?.remove();
    facts.replaceChildren();
    for (const [labelText, valueText] of [
      ['거래금액', `${number.format(item.amount)}원`], ['거래시간대', bankTimeLabel(item.timeBucket)],
      ['자금 종류', bankCodeLabel('자금구분', item.fundType, true)],
      ['이체 방식', bankCodeLabel('매체구분', item.channel, true)],
      ['판별 기록 ID', item.id], ['모델 식별값', item.modelVersion]
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
    const disclaimer = detailOverlay.querySelector('.detail-disclaimer');
    disclaimer.textContent = '기존 4개 필드 이체 모델의 저장 기록입니다. 모델 식별값은 사용한 모델 파일을 구분합니다.';
    const pathKind = contextualKind(item);
    if (pathKind) {
      disclaimer.textContent = `${pathKind === 'v2' ? '입출금 흐름' : '과거 출금 이력'} 모델은 거래일자·계좌 관계·과거 이력을 함께 사용합니다. 낮은 점수는 정상 확정이 아닙니다. 계좌 식별자는 화면에 공개하지 않습니다.`;
      try {
        const evidence = await readContextualEvidence(item);
        if (selectedHistoryRow !== row) return;
        const limited = evidence.supportAssessment?.status !== 'within_observed_support';
        detailOverlay.querySelector('.detail-verdict-label').textContent = limited
          ? `${item.alert ? '경보 · ' : ''}검증 범위 제한` : item.alert ? '경보로 판별했습니다' : '경보 기준 미만입니다';
        for (const [labelText, valueText] of [
          ['거래일자', evidence.transactionDate],
          ['이전 거래 전체', `${number.format(evidence.observedContext?.senderLifetimeCount ?? 0)}건`],
          ['최근 90일 상세 이력', `${number.format(evidence.historyCount)}건`],
          ['같은 수신계좌 이전 거래', `${number.format(evidence.observedContext?.recipientLifetimeCount ?? 0)}건`],
          ['이력 범위', evidence.historyStatus === 'cold_start' ? '이전 거래 없음' : '로컬 저장 기록만 반영'],
          ['검증 범위', supportLabel(evidence)],
        ]) {
          const group = document.createElement('div');
          const term = document.createElement('dt'); term.textContent = labelText;
          const value = document.createElement('dd'); value.textContent = valueText;
          group.append(term, value); facts.append(group);
        }
        if (pathKind === 'v2') {
          for (const [labelText, valueText] of [
            ['출금계좌의 이전 입금', `${number.format(evidence.graphContext?.senderInflow90dCount ?? 0)}건`],
            ['수신계좌의 이전 입금', `${number.format(evidence.graphContext?.recipientInflow90dCount ?? 0)}건`],
            ['수신계좌의 이전 출금', `${number.format(evidence.graphContext?.recipientOutflow90dCount ?? 0)}건`],
          ]) {
            const group = document.createElement('div');
            const term = document.createElement('dt'); term.textContent = labelText;
            const value = document.createElement('dd'); value.textContent = valueText;
            group.append(term, value); facts.append(group);
          }
        }
        const inspect = document.createElement('button');
        inspect.type = 'button'; inspect.className = 'inspect-contextual';
        inspect.textContent = '이 판별의 전체 근거 보기';
        inspect.addEventListener('click', () => { closeDecisionDetail(); document.dispatchEvent(new CustomEvent('dashboard:inspect-contextual', {detail: {id: item.id, kind: pathKind}})); });
        disclaimer.after(inspect);
      } catch {
        if (selectedHistoryRow === row) disclaimer.textContent += ' 과거 맥락 상세를 불러오지 못했습니다.';
      }
    }
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
  const evaluationEvidence = document.createElement('details');
  evaluationEvidence.className = 'evaluation-disclosure';
  evaluationEvidence.id = 'evaluationEvidence';
  evaluationEvidence.innerHTML = '<summary>기존 모델의 검증 수치</summary><p>카드 기준 모델과 기존 4개 필드 이체 모델의 기록입니다. 신규 이체 모델의 성능은 전체 현황과 실시간 판별에서 확인합니다.</p><h3>카드 기준 모델</h3>';
  cardMetrics.setAttribute('aria-label', '카드 모델 검증 지표');
  bankMetrics.setAttribute('aria-label', '이체 모델 검증 지표');
  evaluationEvidence.append(cardMetrics, detectionPanel);
  evaluationEvidence.insertAdjacentHTML('beforeend', '<h3>기존 4개 필드 이체 모델</h3>');
  evaluationEvidence.append(bankMetrics);
  const thresholdDisclosure = document.createElement('details');
  thresholdDisclosure.className = 'evaluation-disclosure';
  thresholdDisclosure.innerHTML = '<summary>기존 모델의 경보 기준 비교</summary>';
  thresholdTable.querySelector('h2').textContent = '경보 기준별 검증 결과';
  thresholdTable.querySelector('th').textContent = '거래 유형';
  thresholdDisclosure.append(thresholdTable);
  cardGrid.remove();
  bank.remove();

  const overview = section('overview', '전체 현황', '로컬 판별 기록, 데이터 변화, 모델 검증 결과를 함께 확인합니다.');
  overview.innerHTML += `
    <div class="overview-toolbar"><span id="overviewUpdated" role="status">불러오는 중</span><button type="button" id="overviewRefresh">현황 새로고침</button></div>
    <div class="overview-kpis">
      <div><span>실시간 이체 기록</span><strong id="overviewCount">—</strong><small>새 이체 모델로 저장한 전체 기록</small></div>
      <div><span>경보 기록</span><strong id="overviewAlerts">—</strong><small id="overviewAlertRate">집계 대기</small></div>
      <div><span>최신 거래에 반영된 이력</span><strong id="overviewDriftCount">—</strong><small>같은 출금계좌의 이전 날짜 기록</small></div>
      <div><span>판별 서비스</span><strong id="overviewService">—</strong><small id="overviewServiceNote">연결 확인 중</small></div>
    </div>
    <div class="overview-grid">
      <article class="panel overview-recent"><div class="section-head"><h2>최근 실시간 판별</h2><button type="button" data-open-view="live">판별·이력 보기</button></div><div class="table-wrap"><table><thead><tr><th>판별 시각</th><th>금액</th><th>모델 점수</th><th>판정</th></tr></thead><tbody id="overviewRecent"><tr><td colspan="4">불러오는 중</td></tr></tbody></table></div><p>새 이체 모델의 저장된 실제 응답 · 가상 입력 포함</p></article>
      <article class="panel"><div class="section-head"><h2>모델 검증 성능</h2><button type="button" id="openEvaluationEvidence" aria-controls="evaluationEvidence" aria-expanded="false">검증 근거</button></div><div class="table-wrap"><table><thead><tr><th>판별 경로</th><th>정밀도</th><th>재현율</th><th>검증 건수</th></tr></thead><tbody id="overviewEvaluation"></tbody></table></div><p>2024년 합성 데이터 · 즉시 판별과 구간 사후 점검은 입력·평가 범위가 다릅니다.</p></article>
      <article class="panel overview-distribution"><div class="section-head"><h2>이체 경보의 검증 결과</h2><button type="button" data-open-view="live">모델 상세</button></div><div id="overviewDrift" class="overview-drift">불러오는 중</div><p>정답 라벨이 있는 2024년 합성 데이터에서 측정했습니다.</p></article>
    </div>
    <details class="dashboard-source"><summary>데이터 출처와 시연 범위</summary><p>카드·이체 모델은 AI Hub 금융거래 합성데이터로 학습했습니다. 카드 거래와 별도 이체 시연 사례는 서버의 결과 파일을, 저장된 이체 판별 기록과 분포는 로컬 DB를 읽습니다.</p></details>`;
  overview.querySelector('.dashboard-source').before(evaluationEvidence, thresholdDisclosure);
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
  document.getElementById('seedMonitoringButton').textContent = '기존 모델에 가상 이체 32건 기록';
  overview.addEventListener('click', event => {
    const target = event.target.closest('[data-open-view]');
    if (target) showView(target.dataset.openView);
  });
  document.getElementById('openEvaluationEvidence').addEventListener('click', event => {
    evaluationEvidence.open = !evaluationEvidence.open;
    event.currentTarget.setAttribute('aria-expanded', String(evaluationEvidence.open));
    if (evaluationEvidence.open) evaluationEvidence.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  });
  evaluationEvidence.addEventListener('toggle', () => {
    document.getElementById('openEvaluationEvidence').setAttribute('aria-expanded', String(evaluationEvidence.open));
  });
  let overviewLoading = false;
  let overviewRefreshPending = false;
  const refreshOverview = async () => {
    if (overviewLoading) { overviewRefreshPending = true; return; }
    overviewLoading = true;
    const button = document.getElementById('overviewRefresh');
    button.disabled = true; button.setAttribute('aria-busy', 'true');
    document.getElementById('overviewUpdated').textContent = '서버에서 현황을 불러오는 중입니다.';
    const urls = ['/api/bank/contextual-decisions?limit=5', '/api/metrics', '/api/bank/contextual-metrics', '/api/model-health',
      '/api/bank/contextual-v2-metrics', '/api/bank/closed-window-metrics', '/api/bank/contextual-v2-capabilities'];
    const results = await Promise.allSettled(urls.map(getJson));
    const [recent, cardReport, bankReport, health, graphReport, windowReport, capability] = results.map(result => result.status === 'fulfilled' ? result.value : null);
    const put = (id, text) => { document.getElementById(id).textContent = text; };
    if (recent) {
      put('overviewCount', `${number.format(recent.totalCount)}건`);
      put('overviewAlerts', `${number.format(recent.totalAlerts)}건`);
      put('overviewAlertRate', recent.totalCount ? `전체 경보 비율 ${percent(recent.totalAlerts / recent.totalCount)}` : '저장된 기록 없음');
      put('overviewDriftCount', recent.items.length ? `${number.format(recent.items[0].observedContext?.senderLifetimeCount ?? 0)}건` : '기록 없음');
    } else {
      for (const id of ['overviewCount', 'overviewAlerts', 'overviewDriftCount']) put(id, '—');
      put('overviewAlertRate', '집계 불러오기 실패');
    }
    document.getElementById('overviewRecent').innerHTML = recent ? recent.items.map(({decision:item,contextStatus}) => `<tr><td title="${escapeHtml(new Date(item.createdAt).toLocaleString('ko-KR'))}">${escapeHtml(compactTimestamp(item.createdAt))}</td><td>${number.format(item.amount)}원</td><td>${(item.riskScore * 100).toFixed(2)}점</td><td><span class="chip ${item.alert ? 'tp' : 'tn'}">${contextStatus === 'cold_start' ? '이력 부족' : item.alert ? '경보' : '경보 기준 미만'}</span></td></tr>`).join('') || '<tr><td colspan="4">저장된 기록이 없습니다.</td></tr>' : '<tr><td colspan="4">판별 이력을 불러오지 못했습니다.</td></tr>';
    const bankValidation = bankReport?.evaluation?.validation;
    const paths = [['카드 기준', cardReport?.validation], ['기본 이체', bankValidation]];
    if (capability?.enabled) paths.push(['연구 이체 · 즉시', graphReport?.evaluation?.validation], ['연구 이체 · 사후', windowReport?.evaluation?.all_2024]);
    document.getElementById('overviewEvaluation').innerHTML = paths.map(([label, validation]) => validation ? `<tr><td>${label}</td><td>${percent(validation.precision)}</td><td>${percent(validation.recall)}</td><td>${number.format(validation.rows)}</td></tr>` : `<tr><td>${label}</td><td colspan="3">검증 결과 불러오기 실패</td></tr>`).join('');
    document.getElementById('overviewDrift').innerHTML = bankValidation ? [['PR-AUC', bankValidation.average_precision.toFixed(3)], ['오탐', `${number.format(bankValidation.confusion?.fp ?? 0)}건`], ['미탐', `${number.format(bankValidation.confusion?.fn ?? 0)}건`]].map(([label,value]) => `<div><span>${label}</span><strong>${value}</strong></div>`).join('') : '새 이체 모델의 검증 결과를 불러오지 못했습니다.';
    const matched = health?.status === 'ok' && bankReport && health.models?.bank_contextual === bankReport.model_version;
    put('overviewService', matched ? '연결됨' : '확인 필요');
    put('overviewServiceNote', matched ? '새 이체 모델과 보고서 일치' : '연결 또는 모델 결과 확인 필요');
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
  const requestedView = location.hash.slice(1);
  showView(['overview', 'workbench', 'history', 'monitoring'].includes(requestedView) ? requestedView : 'overview');
  if (requestedView === 'evaluation') evaluationEvidence.open = true;

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
