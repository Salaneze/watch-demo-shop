/* Витрина Watch Demo.
   Без сборки и фреймворков: три экрана не стоят npm в Python-проекте.
   Разметка строится через createElement, а не innerHTML — названия товаров вводит
   админ, и один <img onerror> в названии превратил бы витрину в чужую площадку. */

const tg = window.Telegram?.WebApp;
const app = document.getElementById('app');

const state = {
  screen: 'catalog',
  catalog: null,
  cart: null,
  orders: null,
  activeCategory: null,
  product: null,
  form: { name: '', phone: '', address: '', comment: '' },
  error: '',
  session: '',      // сессия для клиентов без initData
  authorized: false,
};

/* ---------- мелкие помощники ---------- */

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const child of [].concat(children)) {
    if (child) node.append(child);
  }
  return node;
}

function haptic(kind = 'light') {
  try { tg?.HapticFeedback?.impactOccurred(kind); } catch { /* десктоп без вибро */ }
}

function thumb(url, cls) {
  return url
    ? el('img', { class: cls, src: url, loading: 'lazy', alt: '' })
    : el('div', { class: `${cls} thumb--empty`, text: '⌚' });
}

/* ---------- обращение к API ---------- */

/* Сессия для клиентов, которые не отдают initData (моды Telegram).
   Хранится только в этом браузере и только до истечения срока на сервере. */
const SESSION_KEY = 'watchdemo_session';

function savedSession() {
  try { return localStorage.getItem(SESSION_KEY) || ''; } catch { return ''; }
}

function saveSession(token) {
  try { token ? localStorage.setItem(SESSION_KEY, token) : localStorage.removeItem(SESSION_KEY); }
  catch { /* приватный режим — переживём, сессия просто не запомнится */ }
  state.session = token || '';
}

function authHeader() {
  // Подпись Telegram в заголовке, а не в URL: адреса оседают в логах прокси,
  // а внутри лежат имя и id покупателя.
  if (tg?.initData) return `tma ${tg.initData}`;
  if (state.session) return `tma-session ${state.session}`;
  return 'tma ';
}

async function api(path, options = {}) {
  const res = await fetch(`/api${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'Authorization': authHeader(),
      ...(options.headers || {}),
    },
  });
  if (!res.ok) {
    let detail = `Ошибка ${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch { /* не JSON */ }
    const err = new Error(detail);
    err.status = res.status;
    if (res.status === 401) saveSession('');  // сессия протухла — не тащим её дальше
    throw err;
  }
  return res.json();
}

/* ---------- нижняя кнопка Telegram ---------- */

let mainHandler = null;

function setMain(text, handler, { progress = false } = {}) {
  const btn = tg?.MainButton;
  if (!btn) return;
  if (mainHandler) btn.offClick(mainHandler);
  mainHandler = handler;
  if (!handler) { btn.hide(); return; }
  btn.setText(text);
  btn.onClick(handler);
  progress ? btn.showProgress() : btn.hideProgress();
  btn.show();
}

function go(screen) {
  state.screen = screen;
  state.error = '';
  haptic();
  render();
  window.scrollTo(0, 0);
}

/* ---------- экраны ---------- */

function screenCatalog() {
  const cats = state.catalog.categories.filter(c => c.products.length);
  if (!cats.length) {
    return [el('div', { class: 'empty' }, [el('span', { text: '📦' }),
      el('div', { text: 'Витрина пока пуста' })])];
  }
  if (!cats.some(c => c.id === state.activeCategory)) state.activeCategory = cats[0].id;
  const active = cats.find(c => c.id === state.activeCategory);

  const tabs = el('div', { class: 'tabs' }, cats.map(c =>
    el('button', {
      class: 'tab',
      'aria-selected': String(c.id === state.activeCategory),
      text: c.title,
      onclick: () => { state.activeCategory = c.id; haptic(); render(); },
    })));

  const inCart = new Map((state.cart?.lines || []).map(l => [l.product_id, l.qty]));

  const grid = el('div', { class: 'grid' }, active.products.map(p => {
    const qty = inCart.get(p.id);
    return el('div', { class: 'card-wrap' }, [
      qty ? el('div', { class: 'badge', text: `${qty} шт` }) : null,
      el('div', {
        class: 'card',
        onclick: () => { state.product = p; go('product'); },
      }, [
        thumb(p.image, 'thumb'),
        el('div', { class: 'card-body' }, [
          el('div', { class: 'card-title', text: p.title }),
          el('div', { class: 'card-price', text: p.price_text }),
        ]),
      ]),
    ]);
  }));

  const count = state.cart?.lines?.length || 0;
  setMain(count ? `Корзина · ${state.cart.total_text}` : '', count ? () => go('cart') : null);
  return [tabs, grid];
}

function screenProduct() {
  const p = state.product;
  const line = (state.cart?.lines || []).find(l => l.product_id === p.id);

  if (!state.authorized) {
    // Смотреть товар можно без входа, класть в корзину — нет: корзина привязана
    // к человеку, а кто это, мы ещё не знаем.
    setMain('Войти, чтобы купить', startLogin);
  } else {
    setMain(line ? `В корзине · ${line.qty} шт` : 'Добавить в корзину', async () => {
      setMain('Добавляю…', null, { progress: true });
      try {
        state.cart = await api('/cart', {
          method: 'POST',
          body: JSON.stringify({ product_id: p.id, delta: 1 }),
        });
        haptic('medium');
        render();
      } catch (e) {
        if (e.status === 401) { startLogin(); return; }
        state.error = e.message;
        render();
      }
    });
  }

  return [
    thumb(p.image, 'hero'),
    el('div', { class: 'detail' }, [
      el('h1', { text: p.title }),
      el('div', { class: 'price', text: p.price_text }),
      p.description ? el('p', { text: p.description }) : null,
    ]),
  ];
}

async function patchCart(productId, delta) {
  haptic();
  try {
    state.cart = await api('/cart', {
      method: 'POST',
      body: JSON.stringify({ product_id: productId, delta }),
    });
  } catch (e) {
    if (e.status === 401) { startLogin(); return; }
    state.error = e.message;
  }
  render();
}

function screenCart() {
  const cart = state.cart;
  if (!cart || !cart.lines.length) {
    setMain('К каталогу', () => go('catalog'));
    return [el('div', { class: 'empty' }, [el('span', { text: '🛒' }),
      el('div', { text: 'Корзина пуста' })])];
  }

  const rows = cart.lines.map(l => el('div', { class: 'row' }, [
    thumb(l.image, 'thumb'),
    el('div', { class: 'row-main' }, [
      el('div', { class: 'row-title', text: l.title }),
      el('div', { class: 'row-sum', text: l.sum_text }),
    ]),
    el('div', { class: 'stepper' }, [
      el('button', { text: '−', onclick: () => patchCart(l.product_id, -1) }),
      el('span', { text: String(l.qty) }),
      el('button', { text: '+', onclick: () => patchCart(l.product_id, 1) }),
    ]),
  ]));

  setMain(`Оформить · ${cart.total_text}`, () => go('checkout'));

  return [
    cart.removed?.length
      ? el('div', { class: 'notice notice--bad' },
          [`Сняли с продажи, пока вы выбирали: ${cart.removed.join(', ')}`])
      : null,
    el('div', { class: 'list' }, rows),
    el('div', { class: 'total' }, [
      el('span', { text: 'Итого' }),
      el('span', { text: cart.total_text }),
    ]),
  ];
}

const FIELDS = [
  { key: 'name', label: 'Имя', min: 2, placeholder: 'Как к вам обращаться' },
  { key: 'phone', label: 'Телефон', min: 5, placeholder: '+1 555 000 00 00' },
  { key: 'address', label: 'Адрес доставки', min: 5, placeholder: 'Город, улица, дом, квартира' },
  { key: 'comment', label: 'Комментарий', min: 0, placeholder: 'Необязательно' },
];

function checkoutValid() {
  return FIELDS.every(f => state.form[f.key].trim().length >= f.min);
}

function screenCheckout() {
  const nodes = FIELDS.map(f => {
    const input = el(f.key === 'comment' ? 'textarea' : 'input', {
      placeholder: f.placeholder,
      value: state.form[f.key],
      rows: f.key === 'comment' ? 2 : null,
      oninput: (e) => {
        state.form[f.key] = e.target.value;
        // Перерисовывать поле на каждый символ нельзя — на iOS слетает фокус
        // и закрывается клавиатура. Обновляем только состояние кнопки.
        syncCheckoutButton();
      },
    });
    return el('div', { class: 'field' }, [el('label', { text: f.label }), input]);
  });

  syncCheckoutButton();
  return [el('div', { class: 'section-title', text: 'Доставка' }), ...nodes];
}

function syncCheckoutButton() {
  if (state.screen !== 'checkout') return;
  if (!checkoutValid()) {
    setMain('Заполните поля', null);
    return;
  }
  setMain(`Заказать · ${state.cart.total_text}`, submitOrder);
}

async function submitOrder() {
  setMain('Отправляю…', null, { progress: true });
  try {
    const order = await api('/order', {
      method: 'POST',
      body: JSON.stringify({
        name: state.form.name.trim(),
        phone: state.form.phone.trim(),
        address: state.form.address.trim(),
        comment: state.form.comment.trim(),
        // Сумма, которую покупатель видит прямо сейчас. Если на сервере она уже
        // другая (админ поменял цену), заказ не пройдёт — вместо тихого списания
        // не той суммы вернёмся в корзину.
        expected_total: state.cart.total,
      }),
    });
    state.lastOrder = order;
    state.cart = { lines: [], total: 0, total_text: '', removed: [] };
    tg?.HapticFeedback?.notificationOccurred('success');
    go('done');
  } catch (e) {
    // Заказ мог не пройти из-за изменившейся корзины — показываем её заново,
    // иначе покупатель будет жать кнопку в пустоту.
    try { state.cart = await api('/cart'); } catch { /* покажем что есть */ }
    tg?.HapticFeedback?.notificationOccurred('error');
    state.screen = 'cart';
    state.error = e.message;
    render();
    window.scrollTo(0, 0);
  }
}

function screenDone() {
  const o = state.lastOrder;
  setMain('Вернуться в каталог', () => go('catalog'));
  return [
    el('div', { class: 'empty' }, [
      el('span', { text: '✅' }),
      el('div', { text: `Заказ #${o.id} принят` }),
    ]),
    el('div', { class: 'notice' },
      ['Реквизиты для оплаты бот прислал в чат — вернитесь в переписку и отправьте туда чек.']),
    el('div', { class: 'order' }, [
      el('div', { class: 'order-head' }, [
        el('span', { text: `Заказ #${o.id}` }),
        el('span', { text: o.total_text }),
      ]),
      ...o.items.map(i => el('div', { class: 'order-line', text: `${i.title} — ${i.qty} шт` })),
    ]),
  ];
}

/* ---------- отрисовка ---------- */

function render() {
  app.replaceChildren();

  if (state.error) {
    app.append(el('div', { class: 'notice notice--bad', text: state.error }));
  }

  const screens = {
    catalog: screenCatalog,
    product: screenProduct,
    cart: screenCart,
    checkout: screenCheckout,
    done: screenDone,
  };
  for (const node of screens[state.screen]()) {
    if (node) app.append(node);
  }

  // Своего хедера с крестиком нет: назад — встроенная кнопка Telegram.
  const back = tg?.BackButton;
  if (back) state.screen === 'catalog' || state.screen === 'done' ? back.hide() : back.show();
}

function goBack() {
  const from = { product: 'catalog', cart: 'catalog', checkout: 'cart' };
  go(from[state.screen] || 'catalog');
}

/* Вход для клиентов, которые не передали данные авторизации: модифицированные
   Telegram (AyuGram и подобные) режут initData ради приватности. Личность
   подтверждает сам Telegram — человек жмёт «Старт» в чате, апдейт с одноразовым
   кодом приходит боту, и витрина обменивает код на сессию. Присылать свой id
   из браузера было бы дырой: id прислал бы клиент, а верить клиенту нельзя. */
let pollTimer = null;

async function startLogin() {
  clearInterval(pollTimer);
  state.error = '';
  try {
    const res = await fetch('/api/auth/start', { method: 'POST' });
    if (!res.ok) throw new Error('Вход недоступен');
    const { code, link } = await res.json();

    screenLoginWaiting();
    tg?.openTelegramLink ? tg.openTelegramLink(link) : window.open(link, '_blank');

    let attempts = 0;
    pollTimer = setInterval(async () => {
      attempts += 1;
      if (attempts > 100) { clearInterval(pollTimer); return; }
      try {
        const r = await fetch(`/api/auth/poll?code=${encodeURIComponent(code)}`);
        if (!r.ok) { clearInterval(pollTimer); screenLocked('Код входа устарел'); return; }
        const data = await r.json();
        if (data.ready && data.token) {
          clearInterval(pollTimer);
          saveSession(data.token);
          tg?.HapticFeedback?.notificationOccurred('success');
          boot();
        }
      } catch { /* сеть моргнула — попробуем на следующем тике */ }
    }, 1500);
  } catch (e) {
    screenLocked(e.message);
  }
}

function screenLoginWaiting() {
  app.replaceChildren(el('div', { class: 'empty' }, [
    el('span', { text: '💬' }),
    el('div', { text: 'Ждём подтверждения в чате' }),
  ]));
  app.append(el('div', { class: 'notice' }, [
    'Открылся чат с ботом — нажмите там кнопку «Старт» (или отправьте команду). ' +
    'Витрина подхватит вход сама, возвращаться сюда руками не нужно.',
  ]));
  setMain('', null);
}

function screenLocked(message) {
  const noData = !tg?.initData;
  const version = tg?.version ? `Mini Apps ${tg.version}` : 'клиент без поддержки Mini Apps';

  app.replaceChildren(el('div', { class: 'empty' }, [
    el('span', { text: noData ? '🧩' : '🔒' }),
    el('div', {
      text: noData
        ? 'Ваш клиент Telegram не передал данные авторизации'
        : message,
    }),
  ]));

  if (noData) {
    app.append(el('div', { class: 'notice' }, [
      'Так ведут себя модифицированные клиенты: витрина открывается, но магазин ' +
      'не может понять, кто вы. Это чинится входом через бота — он подтвердит ' +
      'вашу личность сам, вводить ничего не нужно.',
    ]));
    app.append(el('div', { class: 'notice' }, [
      'Каталог ниже доступен и без входа. Вход нужен для корзины и заказов. ' +
      'Ещё можно вернуться в чат — кнопки «Каталог» и «Корзина» работают там полностью.',
    ]));
    app.append(el('div', { class: 'notice', text: `Определился как: ${version}` }));

    // Каталог показываем прямо здесь: смотреть товары можно и без входа.
    if (state.catalog) {
      app.append(el('div', { class: 'section-title', text: 'Каталог' }));
      for (const node of screenCatalog()) {
        if (node) app.append(node);
      }
    }
    // Ставим последней: screenCatalog выше мог повесить на кнопку переход в корзину.
    setMain('Войти через бота', startLogin);
  }
}

async function boot() {
  tg?.ready();
  tg?.expand();
  tg?.BackButton?.onClick(goBack);
  tg?.onEvent?.('themeChanged', () => render());
  state.session = savedSession();

  // Каталог открыт всем: даже без авторизации человек должен увидеть товары,
  // а не пустой экран с отказом.
  try {
    state.catalog = await api('/catalog');
  } catch (e) {
    screenLocked(e.message);
    return;
  }

  try {
    state.cart = await api('/cart');
    state.authorized = true;
  } catch (e) {
    if (e.status !== 401) throw e;
    // Личность не подтверждена: показываем каталог и путь ко входу.
    state.authorized = false;
    state.cart = null;
    screenLocked(e.message);
    return;
  }

  const user = tg?.initDataUnsafe?.user;
  if (user) {
    state.form.name = [user.first_name, user.last_name].filter(Boolean).join(' ');
  }
  render();
}

boot();
