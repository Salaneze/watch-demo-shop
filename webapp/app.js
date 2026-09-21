/* Витрина Watch Demo.
   Без сборки и фреймворков: пять экранов не стоят npm в Python-проекте.
   Разметка строится через createElement, а не innerHTML — названия товаров вводит
   админ, и один <img onerror> в названии превратил бы витрину в чужую площадку. */

const tg = window.Telegram?.WebApp;
const app = document.getElementById('app');

const state = {
  screen: 'catalog',
  catalog: null,
  cart: null,
  orders: null,
  histories: {},              // id заказа -> история, грузится по клику
  openHistory: new Set(),     // раскрытые истории
  lastOrder: null,
  activeCategory: null,
  product: null,
  gallery: 0,                 // текущий кадр на карточке товара
  query: '',
  sort: 'default',
  favorites: new Set(),       // id товаров с сердечком
  onlyFavorites: false,
  delivery: null,             // выбранный код способа доставки
  deliveryOptions: [],
  promo: null,                // ответ /api/promo по последнему введённому коду
  promoInput: '',
  form: { name: '', phone: '', address: '', comment: '' },
  error: '',
  session: '',                // сессия для клиентов без initData
  authorized: false,
  ai: { enabled: false, open: false, messages: [], busy: false },  // консультант; история только на экране
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

/* Текст про остаток. -1 — считать штуки магазин не просил, тогда молчим:
   «в наличии» на товаре без учёта склада обещает то, чего никто не проверял. */
function stockNote(stock) {
  if (stock < 0) return null;
  if (stock === 0) return { text: 'Нет в наличии', low: true };
  if (stock <= 3) return { text: `Осталось ${stock} шт.`, low: true };
  return { text: `В наличии: ${stock} шт.`, low: false };
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

function catalogUrl() {
  const params = new URLSearchParams();
  if (state.query.trim()) params.set('q', state.query.trim());
  if (state.sort !== 'default') params.set('sort', state.sort);
  const qs = params.toString();
  return qs ? `/catalog?${qs}` : '/catalog';
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
  // Статусы меняет админ из бота, а экран заказов у покупателя может висеть
  // открытым — раз в 30 с перечитываем список. Ушёл с экрана — снимаем.
  clearInterval(ordersTimer);
  ordersTimer = screen === 'orders' ? setInterval(refreshOrders, 30000) : null;
}

let ordersTimer = null;

async function refreshOrders() {
  try {
    state.orders = await api('/orders');
    // Раскрытые истории перечитываем тоже: новая запись могла появиться.
    for (const id of state.openHistory) {
      state.histories[id] = (await api(`/orders/${id}`)).history;
    }
    if (state.screen === 'orders') render();
  } catch (e) {
    if (e.status === 401) { clearInterval(ordersTimer); startLogin(); }
  }
}

async function openOrders() {
  state.orders = null;
  go('orders');
  try {
    state.orders = await api('/orders');
  } catch (e) {
    if (e.status === 401) { startLogin(); return; }
    state.error = e.message;
  }
  render();
}

async function toggleHistory(id) {
  haptic();
  if (state.openHistory.has(id)) {
    state.openHistory.delete(id);
    render();
    return;
  }
  try {
    state.histories[id] = (await api(`/orders/${id}`)).history;
    state.openHistory.add(id);
  } catch (e) {
    if (e.status === 401) { startLogin(); return; }
    state.error = e.message;
  }
  render();
}

async function cancelOrder(id) {
  haptic('medium');
  try {
    const fresh = await api(`/orders/${id}/cancel`, { method: 'POST' });
    state.orders = state.orders.map(o => o.id === id ? fresh : o);
    state.histories[id] = fresh.history;
    state.openHistory.add(id);
    tg?.HapticFeedback?.notificationOccurred('success');
  } catch (e) {
    if (e.status === 401) { startLogin(); return; }
    // 409 — статус ушёл дальше, пока экран висел: показываем причину и
    // перечитываем, чтобы кнопка отмены исчезла вместе с правом на неё.
    state.error = e.message;
    await refreshOrders();
  }
  render();
}

/* ---------- поиск и сортировка ---------- */

/* Поле поиска живёт одним и тем же узлом между перерисовками. Пересоздавать его
   нельзя: на iOS новый input теряет фокус и закрывает клавиатуру на первом же
   набранном символе. */
let searchNode = null;
let searchTimer = null;

function searchField() {
  if (!searchNode) {
    searchNode = el('input', {
      class: 'search',
      type: 'search',
      placeholder: 'Поиск по каталогу',
      value: state.query,
      oninput: (e) => {
        state.query = e.target.value;
        // Ждём паузы в наборе: запрос на каждую букву — это десять лишних
        // обращений к серверу ради одного слова.
        clearTimeout(searchTimer);
        searchTimer = setTimeout(reloadCatalog, 300);
      },
    });
  }
  return searchNode;
}

const SORTS = [
  { key: 'default', label: 'по умолчанию' },
  { key: 'price_asc', label: 'сначала дешёвые' },
  { key: 'price_desc', label: 'сначала дорогие' },
  { key: 'title', label: 'по названию' },
];

async function reloadCatalog() {
  try {
    state.catalog = await api(catalogUrl());
    state.error = '';
  } catch (e) {
    state.error = e.message;
  }
  render();
}

async function toggleFavorite(product) {
  if (!state.authorized) { startLogin(); return; }
  haptic();
  try {
    const res = await api('/favorites', {
      method: 'POST',
      body: JSON.stringify({ product_id: product.id }),
    });
    res.is_favorite ? state.favorites.add(product.id) : state.favorites.delete(product.id);
  } catch (e) {
    if (e.status === 401) { startLogin(); return; }
    state.error = e.message;
  }
  render();
}

/* ---------- экраны ---------- */

function hero() {
  return el('div', { class: 'hero' }, [
    el('div', { class: 'hero-brand', text: 'NORDWIND' }),
    el('div', { class: 'hero-tagline', text: 'МЕХАНИЧЕСКИЕ ЧАСЫ' }),
  ]);
}

function filterBar() {
  const sortIndex = SORTS.findIndex(s => s.key === state.sort);
  return el('div', { class: 'filters' }, [
    state.authorized ? el('button', {
      class: 'chip',
      text: '📦 Заказы',
      onclick: () => { haptic(); openOrders(); },
    }) : null,
    el('button', {
      class: `chip${state.onlyFavorites ? ' chip--on' : ''}`,
      text: `♡ Избранное${state.favorites.size ? ` · ${state.favorites.size}` : ''}`,
      onclick: () => { state.onlyFavorites = !state.onlyFavorites; haptic(); render(); },
    }),
    el('button', {
      class: 'chip',
      text: `⇅ ${SORTS[sortIndex < 0 ? 0 : sortIndex].label}`,
      // Переключатель по кругу, а не выпадающий список: вариантов четыре, а
      // нативный <select> в webview выглядит чужеродно на обеих платформах.
      onclick: () => {
        state.sort = SORTS[(sortIndex + 1) % SORTS.length].key;
        haptic();
        reloadCatalog();
      },
    }),
  ]);
}

function productCard(p) {
  const inCart = new Map((state.cart?.lines || []).map(l => [l.product_id, l.qty]));
  const qty = inCart.get(p.id);
  const fav = state.favorites.has(p.id);
  const note = stockNote(p.stock);

  return el('div', { class: 'card-wrap' }, [
    qty ? el('div', { class: 'badge', text: `${qty} шт` }) : null,
    el('button', {
      class: `heart${fav ? ' heart--on' : ''}`,
      'aria-label': fav ? 'Убрать из избранного' : 'В избранное',
      text: fav ? '♥' : '♡',
      onclick: (e) => { e.stopPropagation(); toggleFavorite(p); },
    }),
    el('div', {
      class: 'card',
      onclick: () => { state.product = p; state.gallery = 0; go('product'); },
    }, [
      thumb(p.image, 'thumb'),
      el('div', { class: 'card-body' }, [
        el('div', { class: 'card-title', text: p.title }),
        el('div', { class: 'card-price', text: p.price_text }),
        note && note.low ? el('div', { class: 'card-stock', text: note.text }) : null,
      ]),
    ]),
  ]);
}

function screenCatalog() {
  let cats = state.catalog.categories.filter(c => c.products.length);

  if (state.onlyFavorites) {
    // Фильтр по избранному считается на клиенте: сердечки уже загружены, а
    // отдельный запрос ради подмножества того же каталога — лишний round-trip.
    cats = cats
      .map(c => ({ ...c, products: c.products.filter(p => state.favorites.has(p.id)) }))
      .filter(c => c.products.length);
  }

  const head = [hero(), searchField(), filterBar()];

  if (!cats.length) {
    setMain('', null);
    const what = state.onlyFavorites ? 'Здесь пока пусто — отмечайте товары сердечком'
      : state.query ? `По запросу «${state.query}» ничего не нашлось`
      : 'Витрина пока пуста';
    return [...head, el('div', { class: 'empty' }, [el('span', { text: '🔍' }),
      el('div', { text: what })])];
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

  const grid = el('div', { class: 'grid' }, active.products.map(productCard));

  const count = state.cart?.lines?.length || 0;
  setMain(count ? `Корзина · ${state.cart.total_text}` : '', count ? () => go('cart') : null);
  return [...head, tabs, grid];
}

function gallery(p) {
  const shots = p.images?.length ? p.images : (p.image ? [p.image] : []);
  if (shots.length < 2) return thumb(shots[0] || null, 'hero-img');

  const index = Math.min(state.gallery, shots.length - 1);
  return el('div', { class: 'gallery' }, [
    thumb(shots[index], 'hero-img'),
    el('div', { class: 'dots' }, shots.map((_, i) =>
      el('button', {
        class: `dot${i === index ? ' dot--on' : ''}`,
        'aria-label': `Фото ${i + 1}`,
        onclick: () => { state.gallery = i; haptic(); render(); },
      }))),
  ]);
}

function screenProduct() {
  const p = state.product;
  const line = (state.cart?.lines || []).find(l => l.product_id === p.id);
  const note = stockNote(p.stock);
  const soldOut = p.stock === 0;

  if (!state.authorized) {
    // Смотреть товар можно без входа, класть в корзину — нет: корзина привязана
    // к человеку, а кто это, мы ещё не знаем.
    setMain('Войти, чтобы купить', startLogin);
  } else if (soldOut) {
    setMain('Нет в наличии', null);
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

  const specs = (p.specs || []).length
    ? el('div', { class: 'specs' }, p.specs.map(([k, v]) =>
        el('div', { class: 'spec' }, [
          el('span', { class: 'spec-key', text: k }),
          el('span', { class: 'spec-val', text: v }),
        ])))
    : null;

  return [
    gallery(p),
    el('div', { class: 'detail' }, [
      el('h1', { text: p.title }),
      el('div', { class: 'price', text: p.price_text }),
      note ? el('div', { class: `stock${note.low ? ' stock--low' : ''}`, text: note.text }) : null,
      p.description ? el('p', { text: p.description }) : null,
      specs,
      el('div', { class: 'fineprint' }, [
        el('div', { text: 'Доставка по стране 2–5 дней · гарантия 24 месяца' }),
        el('div', { text: 'Витрина-образец: товары вымышленные, деньги не списываются' }),
      ]),
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

function deliveryCost() {
  const option = state.deliveryOptions.find(o => o.code === state.delivery);
  return option ? option.cost : 0;
}

function promoDiscount() {
  return state.promo?.valid ? state.promo.discount : 0;
}

/* Итог считается и здесь, и на сервере — и это не дублирование, а сверка:
   именно эту сумму мы отправим в expected_total, и если сервер посчитает
   иначе, заказ должен упасть, а не пройти по чужой цене. */
function checkoutTotal() {
  return (state.cart?.total || 0) - promoDiscount() + deliveryCost();
}

let promoNode = null;

function promoField() {
  if (!promoNode) {
    promoNode = el('input', {
      class: 'promo-input',
      placeholder: 'ПРОМОКОД',
      value: state.promoInput,
      oninput: (e) => { state.promoInput = e.target.value; },
    });
  }
  return promoNode;
}

async function applyPromo() {
  haptic();
  try {
    state.promo = await api('/promo', {
      method: 'POST',
      body: JSON.stringify({ code: state.promoInput }),
    });
  } catch (e) {
    if (e.status === 401) { startLogin(); return; }
    state.error = e.message;
  }
  render();
}

function screenCheckout() {
  const fields = FIELDS.map(f => {
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

  const options = el('div', { class: 'options' }, state.deliveryOptions.map(o =>
    el('button', {
      class: `option${o.code === state.delivery ? ' option--on' : ''}`,
      onclick: () => { state.delivery = o.code; haptic(); render(); },
    }, [
      el('div', { class: 'option-main' }, [
        el('div', { class: 'option-title', text: o.title }),
        el('div', { class: 'option-note', text: o.note }),
      ]),
      el('div', { class: 'option-cost', text: o.cost ? o.cost_text : 'бесплатно' }),
    ])));

  const promo = el('div', { class: 'promo' }, [
    promoField(),
    el('button', { class: 'promo-apply', text: 'Применить', onclick: applyPromo }),
  ]);

  const sums = el('div', { class: 'sums' }, [
    sumRow('Товары', state.cart?.total_text || ''),
    state.promo?.valid
      ? sumRow(`Скидка · ${state.promo.code}`, `−${state.promo.discount_text}`)
      : null,
    sumRow(`Доставка · ${deliveryTitle()}`, deliveryCost()
      ? state.deliveryOptions.find(o => o.code === state.delivery).cost_text
      : 'бесплатно'),
    el('div', { class: 'sum-row sum-row--total' }, [
      el('span', { text: 'К ОПЛАТЕ' }),
      el('span', { text: formatMoneyLike(checkoutTotal()) }),
    ]),
  ]);

  syncCheckoutButton();
  return [
    el('div', { class: 'section-title', text: 'Куда и кому' }),
    ...fields,
    el('div', { class: 'section-title', text: 'Доставка' }),
    options,
    el('div', { class: 'section-title', text: 'Промокод' }),
    promo,
    state.promo && !state.promo.valid
      ? el('div', { class: 'notice notice--bad', text: state.promo.message })
      : null,
    sums,
    el('div', { class: 'fineprint' },
      [el('div', { text: 'Оплата по реквизитам после подтверждения заказа' })]),
  ];
}

function deliveryTitle() {
  return state.deliveryOptions.find(o => o.code === state.delivery)?.title || '—';
}

function sumRow(label, value) {
  return el('div', { class: 'sum-row' }, [
    el('span', { text: label }),
    el('span', { text: value }),
  ]);
}

/* Сервер присылает уже отформатированные суммы, но итог со скидкой и доставкой
   складывается на клиенте. Формат берём с образца — из строки корзины, где тот
   же символ валюты уже стоит на своём месте: у одних валют он спереди, у других
   сзади, и угадывать это в вебе нам незачем. */
function formatMoneyLike(value) {
  const sample = state.cart?.total_text || '';
  // Разделитель тысяч — неразрывный пробел, escape-последовательностью:
  // сам символ в коде неотличим от обычного пробела и теряется при копировании.
  const digits = String(value).replace(/\B(?=(\d{3})+(?!\d))/g, '\u00a0');
  const match = sample.match(/^(\D*)[\d\s\u00a0]+(\D*)$/);
  return match ? `${match[1]}${digits}${match[2]}` : digits;
}

function syncCheckoutButton() {
  if (state.screen !== 'checkout') return;
  if (!checkoutValid()) {
    setMain('Заполните поля', null);
    return;
  }
  setMain(`Заказать · ${formatMoneyLike(checkoutTotal())}`, submitOrder);
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
        // Сумма, которую покупатель видит прямо сейчас, вместе со скидкой и
        // доставкой. Если на сервере она другая (админ поменял цену, промокод
        // кончился), заказ не пройдёт — вместо тихого списания не той суммы
        // вернёмся в корзину.
        expected_total: checkoutTotal(),
        // Только код способа и текст промокода: цены считает сервер.
        delivery: state.delivery,
        promo_code: state.promo?.valid ? state.promo.code : '',
      }),
    });
    state.lastOrder = order;
    state.orders = null;  // список заказов перечитается при следующем открытии
    state.cart = { lines: [], total: 0, total_text: '', removed: [] };
    state.promo = null;
    state.promoInput = '';
    promoNode = null;
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

function historyTime(iso) {
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function orderCard(o) {
  const open = state.openHistory.has(o.id);
  const history = open ? (state.histories[o.id] || []) : [];
  return el('div', { class: 'order' }, [
    el('div', { class: 'order-head' }, [
      el('span', { text: `Заказ #${o.id}` }),
      el('span', { text: o.total_text }),
    ]),
    el('div', { class: 'order-status', text: o.status_text }),
    ...o.items.map(i => el('div', { class: 'order-line', text: `${i.title} — ${i.qty} шт` })),
    el('div', { class: 'order-actions' }, [
      el('button', {
        class: `chip${open ? ' chip--on' : ''}`,
        text: open ? 'Скрыть историю' : '🕓 История',
        onclick: () => toggleHistory(o.id),
      }),
      o.can_cancel ? el('button', {
        class: 'chip chip--danger',
        text: 'Отменить заказ',
        onclick: () => cancelOrder(o.id),
      }) : null,
    ]),
    open ? el('div', { class: 'history' }, history.map(h =>
      el('div', { class: 'history-line' }, [
        el('span', { class: 'history-time', text: historyTime(h.created_at) }),
        el('span', { text: h.note ? `${h.status_text} — ${h.note}` : h.status_text }),
      ]))) : null,
  ]);
}

function screenOrders() {
  setMain('', null);
  if (state.orders === null) {
    return [el('div', { class: 'empty' }, [el('span', { text: '⏳' }), el('div', { text: 'Загружаю заказы…' })])];
  }
  if (!state.orders.length) {
    return [el('div', { class: 'empty' }, [el('span', { text: '📦' }), el('div', { text: 'Заказов пока нет' })])];
  }
  return [
    el('div', { class: 'section-title', text: 'Мои заказы' }),
    ...state.orders.map(orderCard),
  ];
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
      o.discount ? el('div', { class: 'order-line', text: `Скидка ${o.promo_code} — −${o.discount_text}` }) : null,
      o.delivery_cost
        ? el('div', { class: 'order-line', text: `${o.delivery_title} — ${o.delivery_cost_text}` })
        : el('div', { class: 'order-line', text: `${o.delivery_title} — бесплатно` }),
    ]),
  ];
}

/* ---------- отрисовка ---------- */

/* ---------- консультант ---------- */

/* Поле ввода — один узел на всё время, по той же причине, что и поиск:
   пересозданный input на iOS теряет фокус вместе с клавиатурой. */
let aiInputNode = null;

function aiInput() {
  if (!aiInputNode) {
    aiInputNode = el('input', {
      class: 'ai-input',
      type: 'text',
      placeholder: 'Спросите про часы…',
      enterkeyhint: 'send',
      maxlength: '500',
      onkeydown: (e) => { if (e.key === 'Enter') sendToAi(); },
    });
  }
  return aiInputNode;
}

async function sendToAi() {
  const text = (aiInputNode?.value || '').trim();
  if (!text || state.ai.busy) return;
  aiInputNode.value = '';
  state.ai.messages.push({ me: true, text });
  state.ai.busy = true;
  haptic();
  render();
  try {
    const res = await api('/ai/chat', { method: 'POST', body: JSON.stringify({ message: text }) });
    state.ai.messages.push({ me: false, text: res.reply });
    state.cart = res.cart;  // модель могла положить товар — бейдж корзины из того же ответа
  } catch (e) {
    if (e.status === 401) { state.ai.busy = false; startLogin(); return; }
    state.ai.messages.push({ me: false, text: e.message, bad: true });
  }
  state.ai.busy = false;
  render();
}

async function resetAi() {
  state.ai.messages = [];
  haptic();
  render();
  try { await api('/ai/reset', { method: 'POST' }); } catch { /* история и так только на экране */ }
}

function toggleAi(open) {
  state.ai.open = open;
  haptic();
  render();
  if (open) requestAnimationFrame(() => aiInputNode?.focus());
}

/* Чат — шторка поверх текущего экрана, а не отдельный экран: каталог под ней
   остаётся, закрыл — продолжаешь листать с того же места. */
function aiPanel() {
  const log = el('div', { class: 'ai-log' }, [
    el('div', { class: 'msg msg--bot', text: 'Я консультант магазина: помогу подобрать часы, расскажу о наличии, положу в корзину.' }),
    ...state.ai.messages.map(m =>
      el('div', { class: `msg ${m.me ? 'msg--me' : 'msg--bot'}${m.bad ? ' msg--bad' : ''}`, text: m.text })),
    state.ai.busy && el('div', { class: 'msg msg--bot msg--typing', text: '…' }),
  ]);
  const bar = el('div', { class: 'ai-bar' }, [
    aiInput(),
    el('button', { class: 'ai-send', text: '➤', 'aria-label': 'Отправить', onclick: sendToAi }),
  ]);
  const head = el('div', { class: 'ai-head' }, [
    el('div', { class: 'ai-title', text: 'Консультант' }),
    el('div', { class: 'ai-head-actions' }, [
      state.ai.messages.length ? el('button', { class: 'chip', text: 'Заново', onclick: resetAi }) : null,
      el('button', { class: 'ai-close', text: '✕', 'aria-label': 'Закрыть', onclick: () => toggleAi(false) }),
    ]),
  ]);

  // Новое сообщение — прокрутка вниз, когда узлы уже в документе.
  requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
  return el('div', { class: 'ai-sheet' }, [
    el('div', { class: 'ai-backdrop', onclick: () => toggleAi(false) }),
    el('div', { class: 'ai-panel' }, [head, log, bar]),
  ]);
}

function aiFab() {
  return el('button', {
    class: 'ai-fab',
    text: '🤖',
    'aria-label': 'Консультант',
    onclick: () => toggleAi(true),
  });
}

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
    orders: screenOrders,
    done: screenDone,
  };
  for (const node of screens[state.screen]()) {
    if (node) app.append(node);
  }

  // Консультант живёт поверх любого экрана. Кнопка — только когда есть кому
  // отвечать и человек вошёл: без подписи API всё равно ответит 401.
  if (state.ai.enabled && state.authorized && state.screen !== 'done') {
    app.append(state.ai.open ? aiPanel() : aiFab());
  }
  // Нижнюю кнопку Telegram прячем под шторкой: она легла бы ровно на поле ввода.
  if (state.ai.open) setMain('', null);

  // Своего хедера с крестиком нет: назад — встроенная кнопка Telegram.
  const back = tg?.BackButton;
  if (back) (state.screen === 'catalog' || state.screen === 'done') && !state.ai.open ? back.hide() : back.show();
}

function goBack() {
  if (state.ai.open) { toggleAi(false); return; }
  const from = { product: 'catalog', cart: 'catalog', checkout: 'cart', orders: 'catalog' };
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

/* Чем именно клиент открыл витрину. Пустая подпись выглядит одинаково у мода
   Telegram, у старого клиента и у незарегистрированного домена, а лечится в трёх
   случаях по-разному — строка внизу экрана избавляет от гадания и по нашей
   витрине, и по клиентской, когда покупатель напишет «не работает». */
function clientDiag() {
  if (!tg) return 'открыто вне Telegram';
  const user = tg.initDataUnsafe?.user;
  return [
    tg.platform || 'платформа неизвестна',
    tg.version ? `Bot API ${tg.version}` : null,
    tg.initData ? `подпись ${tg.initData.length} симв.` : 'подписи нет',
    user ? `профиль есть (id ${user.id})` : 'профиля нет',
  ].filter(Boolean).join(' · ');
}

function screenLocked(message) {
  const noData = !tg?.initData;

  // Отсутствие подписи — штатная развилка, а не поломка, и выглядеть должна так же:
  // покупателю магазина нечего знать про initData и особенности его клиента, а
  // «не передал данные авторизации» под сломанным пазлом читается как «магазин лёг».
  app.replaceChildren(el('div', { class: 'empty' }, [
    el('span', { text: noData ? '🔑' : '🔒' }),
    el('div', { text: noData ? 'Войдите, чтобы оформить заказ' : message }),
  ]));

  if (noData) {
    app.append(el('div', { class: 'notice' }, [
      'Каталог открыт и без входа — смотрите свободно. Вход нужен для корзины и ' +
      'заказа: бот подтвердит вас сам, вводить ничего не нужно.',
    ]));
    app.append(el('div', { class: 'diag', text: clientDiag() }));
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
    state.catalog = await api(catalogUrl());
  } catch (e) {
    screenLocked(e.message);
    return;
  }

  // Способы доставки тоже публичны: цену доставки покупатель вправе видеть до входа.
  try {
    state.deliveryOptions = await api('/delivery');
    if (!state.delivery && state.deliveryOptions.length) {
      state.delivery = state.deliveryOptions[0].code;
    }
  } catch { /* без списка чекаут просто не покажет выбор */ }

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

  try {
    const favorites = await api('/favorites');
    state.favorites = new Set(favorites.map(p => p.id));
  } catch { /* сердечки — не повод не открыть магазин */ }

  try {
    state.ai.enabled = (await api('/ai')).enabled;
  } catch { /* без консультанта витрина остаётся витриной */ }

  const user = tg?.initDataUnsafe?.user;
  if (user) {
    state.form.name = [user.first_name, user.last_name].filter(Boolean).join(' ');
  }
  render();
}

boot();
