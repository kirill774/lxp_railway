// LXP Mini App — app.js
'use strict';

const tg = window.Telegram.WebApp;
tg.expand();
tg.ready();

const API = ''; // относительный путь — бэкенд на том же хосте
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
}

function toast(msg, ms = 2500) {
  const el = $('toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add('hidden'), ms);
}

function markdownToHtml(md) {
  return md
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h2>$1</h2>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>.*<\/li>\n?)+/g, s => `<ul>${s}</ul>`)
    .replace(/^\d+\. (.+)$/gm, '<li>$1</li>')
    .replace(/\n\n/g, '</p><p>')
    .replace(/^(?!<[hul])/gm, '')
    ;
}

async function api(method, path, body = null) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
  };
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
    // Загружаем предметы и цены
    const meta = await api('GET', '/api/subjects');
    state.subjects = meta.subjects;
    state.prices   = meta.prices;

    // Загружаем профиль
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
    // Если initData пустой (разработка) — показываем регистрацию
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
  $('home-group').textContent  = p.group;
  if (state.free) {
    $('free-badge').classList.remove('hidden');
  } else {
    $('free-badge').classList.add('hidden');
  }
  show('screen-home');
}

// ─── Register ────────────────────────────────────────────────────────────────

$('btn-register').addEventListener('click', async () => {
  const name  = $('reg-name').value.trim();
  const group = $('reg-group').value.trim();
  const lxp   = $('reg-lxp').value.trim();

  if (name.length < 2)  { toast('Введи ФИО'); return; }
  if (group.length < 2) { toast('Введи группу'); return; }

  $('btn-register').disabled = true;
  $('btn-register').textContent = 'Сохраняем...';
  try {
    const data = await api('POST', '/api/profile', { init_data: initData, name, group, lxp_login: lxp });
    state.profile = data.profile;
    state.free    = tg.initDataUnsafe?.user?.id && [1016718472].includes(tg.initDataUnsafe.user.id);
    showHome();
    toast('Профиль сохранён');
  } catch (e) {
    toast('Ошибка: ' + e.message);
  } finally {
    $('btn-register').disabled = false;
    $('btn-register').textContent = 'Сохранить профиль';
  }
});

// ─── Home buttons ─────────────────────────────────────────────────────────────

$('btn-new-task').addEventListener('click', () => {
  $('task-subject').value = '';
  $('task-text').value    = '';
  $('urgent-toggle').checked = false;
  $('btn-submit').disabled   = true;
  $('price-amount').textContent = '';
  $('price-type-label').textContent = 'Определяю тип...';
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

// ─── Task screen ─────────────────────────────────────────────────────────────

$('task-back').addEventListener('click', showHome);

// Подсказки предметов
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

// Обновление цены
let priceTimer = null;

function updatePrice() {
  const task   = $('task-text').value.trim();
  const urgent = $('urgent-toggle').checked;
  const subj   = subjectInput.value.trim();

  if (!task && !subj) {
    $('price-amount').textContent = '';
    $('price-type-label').textContent = 'Введи задание';
    $('btn-submit').disabled = true;
    return;
  }

  $('btn-submit').disabled = (task.length < 5 || subj.length < 2);

  clearTimeout(priceTimer);
  priceTimer = setTimeout(async () => {
    try {
      const combined = `${subj}\n${task}`;
      const data = await api('POST', '/api/estimate', { task: combined, urgent });
      $('price-type-label').textContent = data.label;
      if (state.free) {
        $('price-amount').textContent = 'Бесплатно';
      } else {
        $('price-amount').textContent = data.price + '₽';
      }
    } catch (e) { /* тихо */ }
  }, 400);
}

$('task-text').addEventListener('input', updatePrice);
subjectInput.addEventListener('input', updatePrice);
$('urgent-toggle').addEventListener('change', updatePrice);

// Submit
$('btn-submit').addEventListener('click', async () => {
  const subject = subjectInput.value.trim();
  const task    = $('task-text').value.trim();
  const urgent  = $('urgent-toggle').checked;

  if (!subject) { toast('Введи предмет'); return; }
  if (task.length < 5) { toast('Слишком короткое задание'); return; }
  if (task.length > 2000) { toast('Задание слишком длинное (макс 2000)'); return; }

  // Показываем экран обработки
  $('processing-subject').textContent = subject;
  show('screen-processing');

  // Таймер
  let secs = 0;
  $('timer-seconds').textContent = '0';
  state.timerInterval = setInterval(() => {
    secs++;
    $('timer-seconds').textContent = secs;
  }, 1000);

  try {
    const data = await api('POST', '/api/submit', {
      init_data: initData,
      subject,
      task,
      urgent,
    });

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
  const meta = [
    `Предмет: ${subject}`,
    `Задание: ${task.slice(0, 80)}${task.length > 80 ? '...' : ''}`,
    data.free ? 'Стоимость: бесплатно' : `Стоимость: ${data.price}₽`,
  ].join('\n');

  $('result-meta').textContent = meta;
  $('result-text').innerHTML = markdownToHtml(data.answer);

  const payBlock = $('payment-block');
  if (data.payment) {
    $('payment-amount').textContent = data.payment.amount + '₽';
    $('payment-details').innerHTML  =
      `<strong>Карта:</strong> ${data.payment.card}<br>` +
      `<strong>Телефон:</strong> ${data.payment.phone}<br>` +
      `<strong>Получатель:</strong> ${data.payment.name}<br>` +
      `<strong>ID заказа:</strong> ${data.payment.order_id}`;
    payBlock.classList.remove('hidden');

    $('btn-paid').onclick = async () => {
      try {
        await api('POST', '/api/payment_confirm', {
          init_data: initData,
          order_id: data.payment.order_id,
        });
        toast('Спасибо! Оплата принята на проверку.');
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
  $('task-subject').value = '';
  $('task-text').value    = '';
  $('urgent-toggle').checked = false;
  $('btn-submit').disabled   = true;
  $('price-amount').textContent = '';
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

    $('orders-list').innerHTML = orders.reverse().map(o => `
      <div class="order-card">
        <div class="order-subject">${o.subject || 'Без предмета'}</div>
        <div class="order-task">${(o.task || '').slice(0, 80)}</div>
        <div class="order-meta">
          <span>${o.created_at?.slice(0, 10) || ''}</span>
          <span>${o.free
            ? '<span class="order-paid">бесплатно</span>'
            : o.paid
              ? `<span class="order-paid">${o.price}₽ оплачено</span>`
              : `<span class="order-unpaid">${o.price}₽ не оплачено</span>`
          }</span>
        </div>
      </div>
    `).join('');
  } catch (e) {
    $('orders-list').innerHTML = `<div class="empty-state"><p>Ошибка загрузки</p></div>`;
  }
}

$('orders-back').addEventListener('click', showHome);

// ─── Prices ───────────────────────────────────────────────────────────────────

function showPrices() {
  const prices = state.prices;
  const cards  = Object.values(prices).map(p => `
    <div class="price-card">
      <span class="price-card-label">${p.label}</span>
      <span class="price-card-price">${p.rub}₽</span>
    </div>
  `).join('');

  $('prices-list').innerHTML = cards + `
    <div class="price-note">⚡ Срочность (до 24ч) +50%</div>
    <div class="price-note">Оплата переводом после получения работы</div>
    ${state.free ? '<div class="price-note" style="color:var(--btn);font-weight:600">У тебя бесплатный аккаунт ✓</div>' : ''}
  `;
  show('screen-prices');
}

$('prices-back').addEventListener('click', showHome);

// ─── Start ────────────────────────────────────────────────────────────────────

init();
