(() => {
  const root = document.querySelector('main.layout');
  const nav = root?.querySelector('.preview-nav');
  if (!nav) return;

  const tab = document.createElement('button');
  tab.type = 'button';
  tab.dataset.view = 'live';
  tab.textContent = '실시간 이체 판별';
  nav.insertBefore(tab, nav.children[1] || null);

  const view = document.createElement('section');
  view.className = 'preview-view contextual-view';
  view.dataset.view = 'live';
  view.hidden = true;
  view.innerHTML = `
    <div class="view-heading"><div><h2>실시간 이체 판별</h2><p>입력 거래와 서버에 저장된 과거 흐름으로 모델이 새 점수를 계산합니다.</p></div><label class="contextual-model-choice">판별 경로<select id="contextualModel"><option value="v1">과거 출금 이력 · 36개 특징</option><option value="v2">입출금 흐름 · 66개 특징 (연구 후보)</option></select></label></div>
    <div class="contextual-summary" aria-label="모델 검증 현황">
      <div><small>판별 대상</small><strong>합성 이체 데이터의 이상거래 라벨</strong><span>실제 사기 여부를 확정하지 않습니다.</span></div>
      <div><small>2024년 검증 정밀도</small><strong id="contextualPrecision">—</strong><span id="contextualPrecisionNote">결과를 불러오는 중</span></div>
      <div><small>2024년 검증 재현율</small><strong id="contextualRecall">—</strong><span id="contextualRecallNote">결과를 불러오는 중</span></div>
      <div><small>모델 연결</small><strong id="contextualService">확인 중</strong><span id="contextualServiceNote">모델과 보고서 버전 확인 중</span></div>
    </div>
    <div class="contextual-grid">
      <form id="contextualForm" class="panel contextual-form">
        <div class="section-head"><div><h2>거래 입력</h2><p id="contextualInputNote">같은 출금계좌로 이전 날짜 거래를 기록하면 다음 판별에 이력이 반영됩니다. 예시 식별자는 가상 값입니다.</p></div></div>
        <div class="contextual-fields">
          <label>거래일자<input name="date" type="date" required></label>
          <label>출금계좌 식별자<input name="sourceAccount" maxlength="64" pattern="[A-Za-z0-9._~\\-]+" value="DEMO-SENDER-01" required autocomplete="off"></label>
          <label>입금계좌 식별자<input name="destinationAccount" maxlength="64" pattern="[A-Za-z0-9._~\\-]+" value="DEMO-RECEIVER-01" required autocomplete="off"></label>
          <label>출금 금융회사<input name="sourceInstitution" maxlength="64" pattern="[A-Za-z0-9._~\\-]+" value="DEMO-BANK-01" required autocomplete="off"></label>
          <label>입금 금융회사<input name="destinationInstitution" maxlength="64" pattern="[A-Za-z0-9._~\\-]+" value="DEMO-BANK-02" required autocomplete="off"></label>
          <label>거래금액 (원)<input name="amount" type="number" min="0" max="90071992547409" step="0.01" value="100000" required></label>
          <label>거래시간대<select name="timeBucket"><option value="0">00~02시</option><option value="3">03~05시</option><option value="6">06~08시</option><option value="9" selected>09~11시</option><option value="12">12~14시</option><option value="15">15~17시</option><option value="18">18~20시</option><option value="21">21~23시</option></select></label>
          <label>자금 종류<select name="fundType"><option value="0">일반</option><option value="1">급여</option><option value="3">기타</option><option value="4">타행 자동이체</option></select></label>
          <label>이체 방식<select name="channel"><option value="1">PC 뱅킹</option><option value="2" selected>인터넷 뱅킹</option><option value="3">전화</option><option value="4">휴대전화</option><option value="5">건별이체</option><option value="6">기타</option><option value="7">대량이체</option></select></label>
        </div>
        <div class="contextual-actions"><button class="primary" id="contextualSubmit" type="submit" disabled>모델에 판별 요청</button><button type="button" id="contextualNewRequest">같은 조건으로 새 거래 기록</button></div>
        <p class="contextual-hint">과거 내역은 이 브라우저 입력값이 아닌 서버의 저장 기록에서 읽습니다. 거래일자가 2024년 이후면 2024년 검증 지표를 그대로 적용할 근거가 없습니다.</p>
        <p id="contextualMessage" class="contextual-message" role="status">모델 연결을 확인하고 있습니다.</p>
      </form>
      <div class="contextual-side">
        <article class="panel contextual-result" aria-live="polite">
          <div class="section-head"><h2 id="contextualResultTitle">최신 판별</h2><span id="contextualResultState">요청 대기</span></div>
          <div class="contextual-verdict"><strong id="contextualVerdict">—</strong><span id="contextualScore">모델 점수 —</span></div>
          <p id="contextualScoredTransaction" class="contextual-scored-transaction">판별한 거래 —</p>
          <div class="contextual-result-scroll"><p id="contextualEvidenceNote">거래를 보내면 모델의 실제 응답과 저장된 관측 정보를 표시합니다.</p>
          <details id="contextualWindow" class="contextual-window" hidden>
            <summary>같은 시간대 거래 사후 점검</summary>
            <p>입력한 일자·시간대의 서버 저장 거래를 함께 점검합니다. 점검 후 해당 구간의 새 거래 저장은 차단됩니다. 로컬 기록 밖 거래는 포함되지 않습니다.</p>
            <p id="contextualWindowMetrics">검증 지표 확인 중</p>
            <div class="contextual-window-actions"><button id="contextualWindowReview" type="button" disabled>입력 구간 점검</button><button id="contextualWindowLookup" type="button">입력 구간 조회</button><button id="contextualSelectedWindowLookup" type="button" disabled>선택 거래 구간 조회</button></div>
            <p id="contextualWindowStatus" role="status">3시간 구간이 끝난 뒤 실행할 수 있습니다.</p>
            <div id="contextualWindowResults" class="contextual-window-results"></div>
          </details>
          <div id="contextualExplanation" class="contextual-explanation" hidden></div>
          <dl id="contextualEvidence" class="contextual-evidence"></dl></div>
        </article>
        <article class="panel contextual-history">
          <div class="section-head"><div><h2>최근 실시간 판별</h2><p>저장된 새 모델 결과입니다. 기존 4개 필드 판별 기록과 구분합니다.</p></div><div class="contextual-history-actions"><button id="contextualRefresh" type="button">새로고침</button><button id="contextualMore" type="button" hidden>이전 기록 더 보기</button></div></div>
          <div id="contextualRecords" class="contextual-records" aria-live="polite">기록을 불러오는 중입니다.</div>
        </article>
      </div>
    </div>
    <p class="contextual-boundary">이 점수는 AI Hub 합성 데이터의 <code>이상거래여부</code>를 예측한 모델 출력입니다. 표시된 과거 통계는 모델 입력의 관측값이며, 한 거래를 경보로 분류한 인과적 사유나 실거래 정확도는 아닙니다.</p>`;
  nav.after(view);

  const overviewToolbar = root.querySelector('[data-view="overview"] .overview-toolbar');
  if (overviewToolbar) {
    const openLive = document.createElement('button');
    openLive.type = 'button';
    openLive.className = 'contextual-open';
    openLive.textContent = '실시간 이체 판별';
    openLive.addEventListener('click', () => tab.click());
    overviewToolbar.append(openLive);
  }

  const get = id => document.getElementById(id);
  const formatNumber = new Intl.NumberFormat('ko-KR');
  const scoreText = value => `${(Number(value) * 100).toFixed(2)}점 / 100`;
  const pct = value => `${(Number(value) * 100).toFixed(2)}%`;
  let modelKind = 'v1';
  const apiPrefix = () => modelKind === 'v2' ? '/api/bank/contextual-v2' : '/api/bank/contextual';
  const today = new Date();
  get('contextualForm').elements.date.value = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;

  const json = async (url, options) => {
    const response = await fetch(url, options);
    const body = await response.json().catch(() => null);
    if (!response.ok) throw new Error(body?.error || `HTTP ${response.status}`);
    return { body, status: response.status };
  };
  const addFact = (target, label, value) => {
    const row = document.createElement('div');
    const term = document.createElement('dt');
    const detail = document.createElement('dd');
    term.textContent = label;
    detail.textContent = value;
    row.append(term, detail);
    target.append(row);
  };
  const contextLabel = {
    cold_start: '이전 거래 없음',
    prior_day_cold_start: '이전 날짜의 출금 이력 없음',
    local_history_unverified: '로컬 저장 이력만 반영',
  };
  const fundLabels = { '0': '일반', '1': '급여', '3': '기타', '4': '타행 자동이체' };
  const channelLabels = { '1': 'PC 뱅킹', '2': '인터넷 뱅킹', '3': '전화', '4': '휴대전화', '5': '건별이체', '6': '기타', '7': '대량이체' };
  const isCold = evidence => evidence.contextStatus === 'cold_start' || evidence.historyStatus === 'cold_start';
  const limitedSupport = evidence => evidence.supportAssessment && evidence.supportAssessment.status !== 'within_observed_support';
  const verdictText = evidence => limitedSupport(evidence)
    ? `${evidence.decision.alert ? '경보 · ' : ''}검증 범위 제한`
    : isCold(evidence) ? '이력 부족 · 참고 점수' : evidence.decision.alert ? '경보' : '경보 기준 미만';
  const showExplanation = explanation => {
    const target = get('contextualExplanation');
    target.replaceChildren();
    target.hidden = !explanation;
    if (!explanation) return;
    const title = document.createElement('strong');
    title.textContent = '모델 점수에 반영된 정보';
    const summary = document.createElement('p');
    summary.textContent = `정상 학습행 평균·대표값의 비교 점수 ${scoreText(explanation.baselineScore)}에서 아래 정보가 점수를 변화시켰습니다.`;
    target.append(title, summary);
    const groups = [...explanation.groups].sort((a, b) => Math.abs(b.contribution) - Math.abs(a.contribution));
    const list = document.createElement('dl');
    for (const item of groups) {
      const row = document.createElement('div');
      const label = document.createElement('dt');
      const value = document.createElement('dd');
      label.textContent = item.label;
      const points = Number(item.contribution) * 100;
      value.textContent = `${points >= 0 ? '+' : ''}${points.toFixed(2)}점`;
      row.append(label, value);
      list.append(row);
    }
    const note = document.createElement('p');
    note.textContent = '같은 모델을 128개 정보 조합에 실행한 기여도입니다. 비교 기준과 정보 묶음에 따라 달라지며, 사기의 원인을 입증하지 않습니다.';
    target.append(list, note);
  };
  let selectedWindowQuery = null;
  const showEvidence = (evidence, saved = false) => {
    const decision = evidence.decision;
    const previousQuery = selectedWindowQuery;
    selectedWindowQuery = decision.modelVersion?.startsWith('bank-context-v2')
      ? { date: evidence.transactionDate.replaceAll('-', ''), timeBucket: Number(decision.timeBucket) } : null;
    if (previousQuery?.date !== selectedWindowQuery?.date || previousQuery?.timeBucket !== selectedWindowQuery?.timeBucket) resetWindow();
    get('contextualSelectedWindowLookup').disabled = !selectedWindowQuery;
    get('contextualResultTitle').textContent = saved ? '선택한 저장 판별' : '이번 판별';
    get('contextualResultState').textContent = '서버에 저장됨';
    get('contextualScoredTransaction').textContent = `${evidence.transactionDate} · ${String(decision.timeBucket).padStart(2, '0')}~${String(Number(decision.timeBucket) + 2).padStart(2, '0')}시 · ${formatNumber.format(decision.amount)}원`;
    get('contextualVerdict').textContent = verdictText(evidence);
    get('contextualVerdict').classList.toggle('is-alert', decision.alert);
    get('contextualScore').textContent = `모델 점수 ${scoreText(decision.riskScore)} · 기준 ${scoreText(decision.threshold)}`;
    const status = contextLabel[evidence.historyStatus] || evidence.historyStatus || '이력 범위 확인 필요';
    get('contextualEvidenceNote').textContent = limitedSupport(evidence)
      ? '이 입력은 학습 금액·이력·검증 시점 중 일부 범위를 벗어납니다. 점수가 낮아도 정상으로 확정할 수 없습니다.'
      : isCold(evidence)
      ? '이전 거래가 없어 이력 기반 판별의 근거가 부족합니다. 점수가 낮아도 정상 거래로 확정하지 않습니다.'
      : `${status}. ${evidence.graphContext ? '현재 3시간대보다 앞선 입출금 흐름도' : '이전 날짜 기록을'} 반영했습니다. 로컬 기록은 실제 계좌 전체 이력을 뜻하지 않습니다.`;
    showExplanation(evidence.modelExplanation);
    const facts = get('contextualEvidence');
    facts.replaceChildren();
    addFact(facts, '거래일자', evidence.transactionDate || '—');
    addFact(facts, '거래금액', `${formatNumber.format(decision.amount)}원`);
    addFact(facts, '거래시간대', `${String(decision.timeBucket).padStart(2, '0')}~${String(Number(decision.timeBucket) + 2).padStart(2, '0')}시 (코드 ${decision.timeBucket})`);
    addFact(facts, '자금 종류', `${fundLabels[decision.fundType] || '코드 설명 없음'} (코드 ${decision.fundType})`);
    addFact(facts, '이체 방식', `${channelLabels[decision.channel] || '코드 설명 없음'} (코드 ${decision.channel})`);
    addFact(facts, '이전 거래 수 (전체)', `${formatNumber.format(evidence.observedContext?.senderLifetimeCount ?? evidence.historyCount ?? 0)}건`);
    addFact(facts, '최근 90일 상세 이력', `${formatNumber.format(evidence.historyCount ?? 0)}건`);
    if (evidence.observedContext?.recipientLifetimeCount != null) addFact(facts, '같은 수신계좌 과거 거래', `${formatNumber.format(evidence.observedContext.recipientLifetimeCount)}건`);
    if (evidence.observedContext?.distinctRecipientsLifetime != null) addFact(facts, '과거 수신계좌 종류', `${formatNumber.format(evidence.observedContext.distinctRecipientsLifetime)}개`);
    if (evidence.observedContext?.sender90dMeanAmount != null) addFact(facts, '최근 90일 평균 금액', `${formatNumber.format(Math.round(evidence.observedContext.sender90dMeanAmount))}원`);
    addFact(facts, '이력 기준일', `${evidence.historyWindowStart || '—'} 이후 상세 · 거래일 이전`);
    if (evidence.graphContext) {
      addFact(facts, '당일 이전 출금', `${formatNumber.format(evidence.graphContext.senderDayCount || 0)}건`);
      addFact(facts, '출금계좌 최근 90일 입금', `${formatNumber.format(evidence.graphContext.senderInflow90dCount || 0)}건`);
      addFact(facts, '수신계좌 최근 90일 입금', `${formatNumber.format(evidence.graphContext.recipientInflow90dCount || 0)}건`);
    }
    addFact(facts, '사용한 모델', decision.modelVersion?.startsWith('bank-context-v2') ? '입출금 흐름 연구 후보 · 66개 특징' : '과거 출금 이력 모델 · 36개 특징');
  };

  let ready = false;
  const refreshModel = async () => {
    const selectedKind = modelKind;
    ready = false;
    get('contextualSubmit').disabled = true;
    get('contextualService').textContent = '확인 중';
    try {
      const [report, health, capability] = await Promise.all([
        json(`${apiPrefix()}-metrics`), json('/api/model-health'),
        modelKind === 'v2' ? json('/api/bank/contextual-v2-capabilities') : Promise.resolve({ body: { enabled: true } }),
      ]);
      if (selectedKind !== modelKind) return;
      const validation = report.body?.evaluation?.validation;
      if (!validation) throw new Error('새 이체 모델의 검증 보고서가 없습니다.');
      get('contextualPrecision').textContent = pct(validation.precision);
      get('contextualRecall').textContent = pct(validation.recall);
      get('contextualPrecisionNote').textContent = `${formatNumber.format(validation.confusion?.fp ?? 0)}건 오탐 · ${formatNumber.format(validation.rows)}건 평가`;
      get('contextualRecallNote').textContent = `${formatNumber.format(validation.confusion?.fn ?? 0)}건 미탐 · 합성 검증 데이터`;
      const modelKey = modelKind === 'v2' ? 'bank_contextual_v2' : 'bank_contextual';
      const matched = health.body?.status === 'ok' && health.body?.models?.[modelKey] === report.body.model_version;
      ready = matched && capability.body.enabled;
      get('contextualService').textContent = ready ? (modelKind === 'v2' ? '연구 후보 연결됨' : '연결됨') : matched ? '연구 경로 꺼짐' : '버전 확인 필요';
      get('contextualServiceNote').textContent = matched ? '모델 파일과 검증 보고서 일치' : '모델 서비스와 검증 보고서가 일치하지 않습니다.';
      get('contextualMessage').textContent = ready ? '거래를 입력하면 서버가 과거 이력을 읽고 모델에 요청합니다.' : matched ? '연구 후보는 --experimental-bank-v2로 실행한 경우에만 요청할 수 있습니다.' : '모델 버전을 확인할 때까지 판별할 수 없습니다.';
    } catch (error) {
      if (selectedKind !== modelKind) return;
      get('contextualService').textContent = '연결 실패';
      get('contextualServiceNote').textContent = error.message;
      get('contextualMessage').textContent = `판별 준비 실패: ${error.message}`;
    }
    get('contextualSubmit').disabled = !ready;
  };

  let cursor = null;
  let loadingRecords = false;
  let queuedRecords = false;
  const records = get('contextualRecords');
  const more = get('contextualMore');
  const refreshRecords = async append => {
    if (loadingRecords) { if (!append) queuedRecords = true; return; }
    loadingRecords = true;
    more.disabled = true;
    if (!append) records.textContent = '기록을 불러오는 중입니다.';
    try {
      const params = new URLSearchParams({ limit: '20' });
      if (append && cursor) params.set('cursor', cursor);
      const selectedKind = modelKind;
      const { body } = await json(`${apiPrefix()}-decisions?${params}`);
      if (selectedKind !== modelKind) return;
      if (!append) records.replaceChildren();
      for (const item of body.items || []) {
        const row = document.createElement('button');
        row.type = 'button';
        row.className = 'contextual-record';
        const date = new Date(item.decision.createdAt);
        const title = document.createElement('strong');
        title.textContent = `${date.toLocaleString('ko-KR')} · ${formatNumber.format(item.decision.amount)}원`;
        const detail = document.createElement('span');
        detail.textContent = `${verdictText(item)} · ${scoreText(item.decision.riskScore)} · 과거 ${formatNumber.format(item.observedContext?.senderLifetimeCount ?? item.historyCount ?? 0)}건`;
        row.append(title, detail);
        row.addEventListener('click', () => showEvidence(item, true));
        records.append(row);
      }
      if (!records.children.length) records.textContent = '아직 저장된 실시간 판별이 없습니다.';
      cursor = body.nextCursor || null;
      more.hidden = !cursor;
    } catch (error) {
      if (!append) records.textContent = `기록을 불러오지 못했습니다: ${error.message}`;
      else get('contextualMessage').textContent = `이전 기록을 불러오지 못했습니다: ${error.message}`;
    } finally {
      loadingRecords = false;
      more.disabled = false;
      if (queuedRecords) { queuedRecords = false; refreshRecords(false); }
    }
  };
  get('contextualRefresh').addEventListener('click', () => refreshRecords(false));
  more.addEventListener('click', () => refreshRecords(true));

  const form = get('contextualForm');
  let windowLoading = false;
  let windowReady = false;
  let submitting = false;
  const resetWindow = () => {
    get('contextualWindow').hidden = modelKind !== 'v2';
    get('contextualWindow').open = false;
    get('contextualWindowResults').replaceChildren();
    get('contextualWindowStatus').textContent = '3시간 구간이 끝난 뒤 실행할 수 있습니다.';
  };
  const refreshWindowModel = async () => {
    windowReady = false;
    get('contextualWindowReview').disabled = true;
    try {
      const [report, health, capability] = await Promise.all([
        json('/api/bank/closed-window-metrics'), json('/api/model-health'), json('/api/bank/contextual-v2-capabilities'),
      ]);
      const metric = report.body?.evaluation?.all_2024;
      const subtypes = report.body?.evaluation?.anomaly_type;
      if (!metric || !subtypes) throw new Error('사후 점검 검증 보고서가 없습니다.');
      get('contextualWindowMetrics').textContent = `2024년 ${formatNumber.format(metric.rows)}건 · 정밀도 ${pct(metric.precision)} · 재현율 ${pct(metric.recall)}. 분할 거래 ${pct(subtypes['3.0']?.recall ?? 0)}, 동시 거래 ${pct(subtypes['4.0']?.recall ?? 0)} 탐지. 합성 라벨 검증이며 실시간 판별 성능과 다릅니다.`;
      const healthKey = report.body.decision_policy === 'general_or_concurrent_specialist_v1' ? 'bank_closed_window_hybrid' : 'bank_closed_window';
      windowReady = capability.body.enabled && health.body?.models?.[healthKey] === report.body.model_version;
      if (!windowReady) get('contextualWindowStatus').textContent = '사후 점검 연구 경로 또는 모델 연결을 확인해 주세요.';
    } catch (error) {
      get('contextualWindowMetrics').textContent = `사후 점검 준비 실패: ${error.message}`;
    }
    get('contextualWindowReview').disabled = !windowReady || windowLoading;
  };
  const reviewWindow = async (save, selected = false) => {
    if (windowLoading || submitting || (save && !windowReady)) return;
    if (selected && (!selectedWindowQuery || modelKind !== 'v2')) return;
    const date = selected ? selectedWindowQuery.date : form.elements.date.value.replaceAll('-', '');
    const timeBucket = selected ? selectedWindowQuery.timeBucket : Number(form.elements.timeBucket.value);
    if (!/^\d{8}$/.test(date)) { get('contextualWindowStatus').textContent = '거래일자를 입력해 주세요.'; return; }
    windowLoading = true;
    get('contextualWindowReview').disabled = true;
    get('contextualWindowLookup').disabled = true;
    get('contextualSelectedWindowLookup').disabled = true;
    get('contextualModel').disabled = true;
    get('contextualSubmit').disabled = true;
    form.elements.date.disabled = true;
    form.elements.timeBucket.disabled = true;
    get('contextualWindowStatus').textContent = save ? '저장된 구간 전체를 모델로 점검하고 있습니다.' : '저장된 사후 점검을 불러오고 있습니다.';
    get('contextualWindowResults').replaceChildren();
    try {
      const response = save ? await json('/api/bank/closed-window-reviews', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ date, timeBucket }),
      }) : await json(`/api/bank/closed-window-reviews?${new URLSearchParams({ date, timeBucket: String(timeBucket) })}`);
      const result = response.body;
      get('contextualWindowStatus').textContent = `${result.date} ${String(result.timeBucket).padStart(2, '0')}시 구간 · ${formatNumber.format(result.observedRows)}건 중 ${formatNumber.format(result.alertCount)}건 경보 · 저장 결과`;
      const list = document.createElement('ol');
      for (const item of result.items || []) {
        const row = document.createElement('li');
        const limited = item.supportAssessment?.status !== 'within_observed_support';
        const hybrid = result.decisionPolicy === 'general_or_concurrent_specialist_v1';
        const trigger = hybrid ? [item.generalAlert && '일반 패턴', item.concurrentAlert && '동시 거래 패턴'].filter(Boolean).join(' · ') : '';
        const scores = hybrid ? `일반 ${scoreText(item.riskScore)} / 동시 거래 ${scoreText(item.concurrentScore)}` : scoreText(item.riskScore);
        row.textContent = `${formatNumber.format(item.amount)}원 · ${scores} · ${item.alert ? `경보${trigger ? ` (${trigger})` : ''}` : '경보 기준 미만'}${limited ? ' · 검증 범위 제한' : ''}`;
        list.append(row);
      }
      get('contextualWindowResults').append(list);
      if (result.decisionPolicy === 'general_or_concurrent_specialist_v1') {
        const note = document.createElement('p');
        note.textContent = `일반 기준 ${scoreText(result.threshold)} · 동시 거래 기준 ${scoreText(result.specialistThreshold)}. 어느 한 기준을 넘으면 경보이며, 두 점수는 사기 확률을 뜻하지 않습니다.`;
        get('contextualWindowResults').append(note);
      }
    } catch (error) {
      get('contextualWindowStatus').textContent = `사후 점검 ${save ? '요청' : '조회'} 실패: ${error.message}`;
    } finally {
      windowLoading = false;
      get('contextualWindowReview').disabled = !windowReady;
      get('contextualWindowLookup').disabled = false;
      get('contextualSelectedWindowLookup').disabled = !selectedWindowQuery || modelKind !== 'v2';
      get('contextualModel').disabled = false;
      get('contextualSubmit').disabled = !ready;
      form.elements.date.disabled = false;
      form.elements.timeBucket.disabled = false;
    }
  };
  get('contextualWindowReview').addEventListener('click', () => reviewWindow(true));
  get('contextualWindowLookup').addEventListener('click', () => reviewWindow(false));
  get('contextualSelectedWindowLookup').addEventListener('click', () => reviewWindow(false, true));
  form.elements.date.addEventListener('input', resetWindow);
  form.elements.timeBucket.addEventListener('change', resetWindow);
  let requestKey = crypto.randomUUID();
  const changeModel = async kind => {
    modelKind = kind;
    get('contextualModel').value = kind;
    selectedWindowQuery = null;
    get('contextualSelectedWindowLookup').disabled = true;
    resetWindow();
    if (modelKind === 'v2') refreshWindowModel();
    get('contextualInputNote').textContent = modelKind === 'v2'
      ? '현재 3시간대보다 앞선 입출금 흐름을 반영합니다. 전체 원장은 시간순으로 기록하며 예시 식별자는 가상 값입니다.'
      : '같은 출금계좌로 이전 날짜 거래를 기록하면 다음 판별에 이력이 반영됩니다. 예시 식별자는 가상 값입니다.';
    cursor = null;
    requestKey = crypto.randomUUID();
    get('contextualResultState').textContent = '요청 대기';
    get('contextualResultTitle').textContent = '최신 판별';
    get('contextualScoredTransaction').textContent = '판별한 거래 —';
    get('contextualVerdict').textContent = '—';
    get('contextualVerdict').classList.remove('is-alert');
    get('contextualScore').textContent = '모델 점수 —';
    get('contextualEvidence').replaceChildren();
    showExplanation(null);
    get('contextualEvidenceNote').textContent = '선택한 경로의 모델과 저장 이력을 불러옵니다. 경로별 원장과 결과는 별도로 보존됩니다.';
    await refreshModel();
    refreshRecords(false);
  };
  get('contextualModel').addEventListener('change', event => changeModel(event.target.value));
  form.addEventListener('input', () => {
    requestKey = crypto.randomUUID();
    get('contextualMessage').textContent = '입력값이 변경되었습니다. 판별 요청 전까지 오른쪽 결과는 이전에 저장된 거래의 결과입니다.';
  });
  get('contextualNewRequest').addEventListener('click', () => {
    requestKey = crypto.randomUUID();
    get('contextualMessage').textContent = '같은 입력으로 별도 거래를 기록할 수 있습니다.';
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (windowLoading || submitting || !ready || !form.reportValidity()) return;
    submitting = true;
    const values = new FormData(form);
    const transaction = {
      '거래일자': String(values.get('date')).replaceAll('-', ''),
      '출금계좌일련번호': String(values.get('sourceAccount')).trim(),
      '입금계좌일련번호': String(values.get('destinationAccount')).trim(),
      '출금금융회사일련번호': String(values.get('sourceInstitution')).trim(),
      '입금금융회사일련번호': String(values.get('destinationInstitution')).trim(),
      '거래금액': Number(values.get('amount')),
      '거래시간대': Number(values.get('timeBucket')),
      '자금구분': String(values.get('fundType')),
      '매체구분': String(values.get('channel')),
    };
    const button = get('contextualSubmit');
    get('contextualWindowReview').disabled = true;
    get('contextualWindowLookup').disabled = true;
    get('contextualSelectedWindowLookup').disabled = true;
    button.disabled = true;
    get('contextualModel').disabled = true;
    button.setAttribute('aria-busy', 'true');
    get('contextualMessage').textContent = '서버가 이전 거래를 모아 모델에 판별을 요청하고 있습니다.';
    try {
      const result = await json(`${apiPrefix()}-events`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': requestKey },
        body: JSON.stringify(transaction),
      });
      showEvidence(result.body);
      get('contextualMessage').textContent = result.status === 201 ? '모델 응답과 거래를 저장했습니다.' : '같은 요청의 저장된 판별을 다시 불러왔습니다.';
      await refreshRecords(false);
      document.dispatchEvent(new Event('dashboard:data-changed'));
    } catch (error) {
      get('contextualMessage').textContent = `판별하지 못했습니다: ${error.message}. 같은 입력을 다시 보내면 동일한 요청 키를 사용합니다.`;
    } finally {
      submitting = false;
      button.disabled = !ready;
      get('contextualWindowReview').disabled = !windowReady;
      get('contextualWindowLookup').disabled = false;
      get('contextualSelectedWindowLookup').disabled = !selectedWindowQuery || modelKind !== 'v2';
      get('contextualModel').disabled = false;
      button.removeAttribute('aria-busy');
    }
  });

  refreshModel();
  refreshRecords(false);
  document.addEventListener('dashboard:inspect-contextual', async event => {
    const requestedKind = event.detail.kind === 'v2' ? 'v2' : 'v1';
    if (modelKind !== requestedKind) {
      await changeModel(requestedKind);
    }
    tab.click();
    get('contextualMessage').textContent = '저장된 거래의 이력 근거를 불러오는 중입니다.';
    try {
      const result = await json(`${apiPrefix()}-decisions/${encodeURIComponent(event.detail.id)}`);
      showEvidence(result.body, true);
      get('contextualMessage').textContent = '선택한 거래의 저장된 모델 응답을 불러왔습니다.';
    } catch (error) {
      get('contextualMessage').textContent = `상세 조회 실패: ${error.message}`;
    }
  });
  if (location.hash === '#live') tab.click();
})();
