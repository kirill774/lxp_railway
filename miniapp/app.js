// LXP Mini App — app.js v2
'use strict';

const tg = window.Telegram.WebApp;
tg.expand();
tg.ready();
tg.setHeaderColor('#0f0f1a');
tg.setBackgroundColor('#0f0f1a');

const API = '';
let initData = tg.initData || '';
let state = {
  profile: null,
  free: false,
  subjects: [],
  prices: {},
  currentResult: null,
  timerInterval: null,
};

// ─── Utils ────────────────────────────────────────────────────────────────────

function $(id) { return document.getElementById(id); }

function show(screenId) {
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  $(screenId).classList.add('active');
  window.scrollTo(0, 0);
}

function toast(msg, ms = 2800) {
  const el = $('toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add('hidden'), ms);
}

function starsLabel(stars, free) {
  if (free) return 'Бесплатно ✓';
  return stars + ' ⭐';
}

function rubLabel(rub) {
  return '≈ ' + rub + '₽';
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function markdownToHtml(md) {
  return escapeHtml(md)
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm,  '<h2>$1</h2>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>.*<\/li>\n?)+/g, s => `<ul>${s}</ul>`)
    .replace(/^\d+\. (.+)$/gm, '<li>$1</li>')
    .replace(/\n\n/g, '</p><p>');
}

async function api(method, path, body = null) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(API + path, opts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

// ─── Init ─────────────────────────────────────────────────────────────────────

async function init() {
  show('screen-loading');
  try {
    const meta = await api('GET', '/api/subjects');
    state.subjects = meta.subjects;
    state.prices   = meta.prices;

    const encoded = encodeURIComponent(initData);
    const data = await api('GET', `/api/profile?init_data=${encoded}`);
    state.profile = data.profile;
    state.free    = data.free;

    if (state.profile) {
      showHome();
    } else {
      show('screen-register');
    }
  } catch (e) {
    console.error('Init error:', e);
    show('screen-register');
  }
}

// ─── Home ─────────────────────────────────────────────────────────────────────

function showHome() {
  const p = state.profile;
  if (!p) { show('screen-register'); return; }
  const initials = p.name.split(' ').map(w => w[0]).join('').slice(0, 2).toUpperCase();
  $('home-avatar').textContent = initials;
  $('home-name').textContent   = p.name;
  $('home-group').textContent  = '📚 ' + p.group;
  if (state.free) {
    $('free-badge').classList.remove('hidden');
  } else {
    $('free-badge').classList.add('hidden');
  }
  show('screen-home');
}

// ─── Register ─────────────────────────────────────────────────────────────────

$('btn-register').addEventListener('click', async () => {
  const name  = $('reg-name').value.trim();
  const group = $('reg-group').value.trim();
  const lxp   = $('reg-lxp').value.trim();

  if (name.length < 2)  { toast('Введи ФИО'); return; }
  if (group.length < 2) { toast('Введи группу'); return; }

  const btn = $('btn-register');
  btn.disabled = true;
  btn.textContent = 'Сохраняем...';
  try {
    const data = await api('POST', '/api/profile', { init_data: initData, name, group, lxp_login: lxp });
    state.profile = data.profile;
    state.free    = tg.initDataUnsafe?.user?.id && [1016718472].includes(tg.initDataUnsafe.user.id);
    showHome();
    toast('Профиль сохранён ✓');
  } catch (e) {
    toast('Ошибка: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Сохранить профиль';
  }
});

// ─── Home buttons ─────────────────────────────────────────────────────────────

$('btn-new-task').addEventListener('click', () => {
  $('task-subject').value       = '';
  $('task-text').value          = '';
  $('urgent-toggle').checked    = false;
  $('btn-submit').disabled      = true;
  $('price-amount').textContent = '';
  $('price-rub').textContent    = '';
  $('price-type-label').textContent = 'Введи задание';
  show('screen-task');
});

$('btn-orders').addEventListener('click', loadOrders);
$('btn-prices').addEventListener('click', showPrices);

$('btn-profile-edit').addEventListener('click', () => {
  if (state.profile) {
    $('reg-name').value  = state.profile.name  || '';
    $('reg-group').value = state.profile.group || '';
    $('reg-lxp').value   = state.profile.lxp_login || '';
  }
  show('screen-register');
});

// ─── Task screen ──────────────────────────────────────────────────────────────

$('task-back').addEventListener('click', showHome);

const subjectInput = $('task-subject');
const suggestions  = $('subject-suggestions');

subjectInput.addEventListener('input', () => {
  const q = subjectInput.value.toLowerCase().trim();
  suggestions.innerHTML = '';
  if (!q) { suggestions.classList.add('hidden'); return; }
  const matches = state.subjects.filter(s => s.toLowerCase().includes(q));
  if (!matches.length) { suggestions.classList.add('hidden'); return; }
  matches.forEach(s => {
    const div = document.createElement('div');
    div.className = 'suggestion-item';
    div.textContent = s;
    div.addEventListener('click', () => {
      subjectInput.value = s;
      suggestions.classList.add('hidden');
      updatePrice();
    });
    suggestions.appendChild(div);
  });
  suggestions.classList.remove('hidden');
});

document.addEventListener('click', e => {
  if (!subjectInput.contains(e.target) && !suggestions.contains(e.target)) {
    suggestions.classList.add('hidden');
  }
});

let priceTimer = null;

function updatePrice() {
  const task   = $('task-text').value.trim();
  const urgent = $('urgent-toggle').checked;
  const subj   = subjectInput.value.trim();

  const ready = task.length >= 5 && subj.length >= 2;
  $('btn-submit').disabled = !ready;

  if (!task && !subj) {
    $('price-amount').textContent = '';
    $('price-rub').textContent    = '';
    $('price-type-label').textContent = 'Введи задание';
    return;
  }

  clearTimeout(priceTimer);
  priceTimer = setTimeout(async () => {
    try {
      const combined = `${subj}\n${task}`;
      const data = await api('POST', '/api/estimate', { task: combined, urgent });
      $('price-type-label').textContent = data.label;
      if (state.free) {
        $('price-amount').textContent = 'Бесплатно ✓';
        $('price-rub').textContent    = '';
      } else {
        $('price-amount').textContent = data.stars + ' ⭐';
        $('price-rub').textContent    = '≈ ' + data.price + '₽';
      }
    } catch (e) { /* тихо */ }
  }, 400);
}

$('task-text').addEventListener('input', updatePrice);
subjectInput.addEventListener('input', updatePrice);
$('urgent-toggle').addEventListener('change', updatePrice);

$('btn-submit').addEventListener('click', async () => {
  const subject = subjectInput.value.trim();
  const task    = $('task-text').value.trim();
  const urgent  = $('urgent-toggle').checked;

  if (!subject) { toast('Введи предмет'); return; }
  if (task.length < 5) { toast('Слишком короткое задание'); return; }
  if (task.length > 2000) { toast('Задание слишком длинное (макс 2000)'); return; }

  $('processing-subject').textContent = subject;
  show('screen-processing');

  let secs = 0;
  $('timer-seconds').textContent = '0';
  state.timerInterval = setInterval(() => {
    secs++;
    $('timer-seconds').textContent = secs;
  }, 1000);

  try {
    const data = await api('POST', '/api/submit', { init_data: initData, subject, task, urgent });
    clearInterval(state.timerInterval);
    state.currentResult = data;
    showResult(data, subject, task);
  } catch (e) {
    clearInterval(state.timerInterval);
    toast('Ошибка: ' + e.message, 4000);
    show('screen-task');
  }
});

// ─── Result ───────────────────────────────────────────────────────────────────

function showResult(data, subject, task) {
  const starsAmt = data.stars || 0;
  const rubAmt   = data.price || 0;
  const costLine = data.free
    ? 'Стоимость: бесплатно ✓'
    : `Стоимость: ${starsAmt} ⭐ (≈${rubAmt}₽)`;

  $('result-meta').innerHTML =
    `<span class="meta-row">📖 ${escapeHtml(subject)}</span>` +
    `<span class="meta-row">📝 ${escapeHtml(task.slice(0, 80))}${task.length > 80 ? '…' : ''}</span>` +
    `<span class="meta-row cost-row">${escapeHtml(costLine)}</span>`;

  $('result-text').innerHTML = markdownToHtml(data.answer);

  const payBlock = $('payment-block');
  if (data.payment) {
    const p = data.payment;
    $('payment-amount').innerHTML =
      `<span class="stars-big">${escapeHtml(p.stars)} ⭐</span><span class="rub-small">≈ ${escapeHtml(p.amount)}₽</span>`;
    $('payment-details').innerHTML =
      `<div class="pay-row"><span>Карта</span><strong>${escapeHtml(p.card)}</strong></div>` +
      `<div class="pay-row"><span>Телефон</span><strong>${escapeHtml(p.phone)}</strong></div>` +
      `<div class="pay-row"><span>Получатель</span><strong>${escapeHtml(p.name)}</strong></div>` +
      `<div class="pay-row"><span>ID заказа</span><code>${escapeHtml(p.order_id)}</code></div>`;
    payBlock.classList.remove('hidden');

    $('btn-paid').onclick = async () => {
      try {
        await api('POST', '/api/payment_confirm', { init_data: initData, order_id: p.order_id });
        toast('Спасибо! Оплата на проверке ✓');
        payBlock.classList.add('hidden');
      } catch (e) {
        toast('Ошибка: ' + e.message);
      }
    };
  } else {
    payBlock.classList.add('hidden');
  }

  show('screen-result');
}

$('result-back').addEventListener('click', showHome);
$('btn-new-after').addEventListener('click', () => {
  $('task-subject').value       = '';
  $('task-text').value          = '';
  $('urgent-toggle').checked    = false;
  $('btn-submit').disabled      = true;
  $('price-amount').textContent = '';
  $('price-rub').textContent    = '';
  show('screen-task');
});

// ─── Orders ───────────────────────────────────────────────────────────────────

async function loadOrders() {
  show('screen-orders');
  $('orders-list').innerHTML = '<div class="loader-wrap"><div class="spinner"></div></div>';
  try {
    const encoded = encodeURIComponent(initData);
    const data = await api('GET', `/api/orders?init_data=${encoded}`);
    const orders = data.orders || [];

    if (!orders.length) {
      $('orders-list').innerHTML = `
        <div class="empty-state">
          <div class="empty-icon">📋</div>
          <p>Заказов пока нет</p>
          <p>Создай первое задание</p>
        </div>`;
      return;
    }

    $('orders-list').innerHTML = [...orders].reverse().map(o => {
      const costStr = o.free
        ? '<span class="order-paid">бесплатно</span>'
        : o.paid
          ? `<span class="order-paid">${o.stars || o.price} ${o.stars ? '⭐' : '₽'} оплачено</span>`
          : `<span class="order-unpaid">${o.stars || o.price} ${o.stars ? '⭐' : '₽'} не оплачено</span>`;
      return `
        <div class="order-card">
          <div class="order-subject">${escapeHtml(o.subject || 'Без предмета')}</div>
          <div class="order-task">${escapeHtml((o.task || '').slice(0, 80))}</div>
          <div class="order-meta">
            <span>${escapeHtml((o.created_at || '').slice(0, 10))}</span>
            ${costStr}
          </div>
        </div>`;
    }).join('');
  } catch (e) {
    $('orders-list').innerHTML = `<div class="empty-state"><p>Ошибка загрузки</p></div>`;
  }
}

$('orders-back').addEventListener('click', showHome);

// ─── Prices ───────────────────────────────────────────────────────────────────

function showPrices() {
  const prices = state.prices;
  const cards = Object.values(prices).map(p => `
    <div class="price-card">
      <span class="price-card-label">${escapeHtml(p.label)}</span>
      <div class="price-card-right">
        <span class="price-card-stars">${escapeHtml(p.stars)} ⭐</span>
        <span class="price-card-rub">≈ ${escapeHtml(p.rub)}₽</span>
      </div>
    </div>
  `).join('');

  $('prices-list').innerHTML = cards + `
    <div class="price-note">⚡ Срочность (до 24ч) +50%</div>
    <div class="price-note">Оплата — Telegram Stars прямо в боте</div>
    <div class="price-note">Курс: 1 ⭐ ≈ 1.3₽</div>
    ${state.free ? '<div class="price-note free-note">У тебя бесплатный аккаунт ✓</div>' : ''}
  `;
  show('screen-prices');
}

$('prices-back').addEventListener('click', showHome);

// ─── Start ────────────────────────────────────────────────────────────────────

init();
