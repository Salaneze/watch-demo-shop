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
  contact: '',                // подписанный ответ requestContact; номер в form.phone только для показа
  homeScreen: 'unsupported',  // ответ checkHomeScreenStatus: added / missed / unknown / unsupported
  error: '',
  session: '',                // сессия для клиентов без initData
  authorized: false,
  ai: { enabled: false, open: false, messages: [], busy: false },  // консультант; история только на экране
  reviews: {},                // id товара -> отзывы; undefined — ещё не грузили
  related: {},                // 'p<id>' | 'cart' -> { from_orders, products } или 'loading'
  reviewForm: null,           // { order_id, product_id, rating, text } — открытая форма в заказах
  notice: '',                 // нейтральное сообщение над экраном (не ошибка)
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

/* Каждый новый метод Telegram сначала через версию клиента: на старом SDK метод
   может быть объявлен, но бросает WebAppMethodUnsupported, и витрина падает
   целиком из-за одной необязательной кнопки. */
function supports(version) {
  try { return Boolean(tg?.isVersionAtLeast?.(version)); } catch { return false; }
}

function haptic(kind = 'light') {
  try { tg?.HapticFeedback?.impactOccurred(kind); } catch { /* десктоп без вибро */ }
}

function thumb(url, cls) {
  return url
    ? el('img', { class: cls, src: url, loading: 'lazy', alt: '' })
    : el('div', { class: `${cls} thumb--empty`, text: '⌚' });
}

/* «★ 4.6 · 12» — без оценок ничего: пустые звёзды на новинке читаются как
   «товар плохой», а не как «товар новый». */
function ratingBadge(rating, count, cls = 'rating') {
  if (!count) return null;
  return el('div', { class: cls, text: `★ ${rating.toFixed(1)} · ${count}` });
}

function plural(n, one, few, many) {
  const d = n % 10, h = n % 100;
  if (d === 1 && h !== 11) return one;
  if (d >= 2 && d <= 4 && (h < 12 || h > 14)) return few;
  return many;
}

function stars(n) {
  return '★'.repeat(n) + '☆'.repeat(5 - n);
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

/* Код входа, который ждёт подтверждения в чате. На десктопе переход в чат с
   ботом закрывает витрину, опрос умирает вместе с ней, и подтверждённый вход
   никто не забирал: бот писал «авторизована», а новая витрина снова просила
   войти. Теперь код живёт и между открытиями, пока жив на сервере (5 минут). */
const PENDING_KEY = 'watchdemo_login_code';
const PENDING_TTL_MS = 5 * 60 * 1000;

function rememberPending(code) {
  try {
    code ? localStorage.setItem(PENDING_KEY, JSON.stringify({ code, at: Date.now() }))
         : localStorage.removeItem(PENDING_KEY);
  } catch { /* без хранилища останется старый путь — опрос из открытой витрины */ }
}

async function pickUpPendingLogin() {
  let pending = null;
  try { pending = JSON.parse(localStorage.getItem(PENDING_KEY) || 'null'); } catch { return; }
  if (!pending?.code) return;
  if (Date.now() - pending.at > PENDING_TTL_MS) { rememberPending(null); return; }
  try {
    const r = await fetch(`/api/auth/poll?code=${encodeURIComponent(pending.code)}`);
    if (!r.ok) { rememberPending(null); return; }
    const data = await r.json();
    if (data.ready && data.token) {
      rememberPending(null);
      saveSession(data.token);
    }
    // Не подтверждён ещё — код оставляем: человек мог открыть витрину раньше,
    // чем нажал кнопку в чате.
  } catch { /* сеть — попробуем при следующем открытии */ }
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

// Вторая нижняя кнопка (Bot API 7.10+). На старых клиентах её нет — экран
// товара тогда рисует «Добавить ещё» у себя в разметке, см. screenProduct.
let secondaryHandler = null;

function setSecondary(text, handler) {
  const btn = tg?.SecondaryButton;
  if (!btn) return false;
  if (secondaryHandler) btn.offClick(secondaryHandler);
  secondaryHandler = handler;
  if (!handler) { btn.hide(); return true; }
  btn.setText(text);
  btn.onClick(handler);
  btn.show();
  return true;
}

function go(screen) {
  state.screen = screen;
  state.error = '';
  state.notice = '';
  if (screen === 'cart' && state.authorized && state.cart?.lines?.length) {
    loadRelated('cart', '/cart/related');
  }
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
  // «Каталог» и «Избранное» — переключатель из двух положений, а не флажок:
  // с одним чипом «Избранное» человек не догадывался, что выход — нажать его ещё раз.
  return el('div', { class: 'filters' }, [
    el('button', {
      class: `chip${state.onlyFavorites ? '' : ' chip--on'}`,
      text: '☰ Каталог',
      onclick: () => { state.onlyFavorites = false; haptic(); render(); },
    }),
    el('button', {
      class: `chip${state.onlyFavorites ? ' chip--on' : ''}`,
      text: `♡ Избранное${state.favorites.size ? ` · ${state.favorites.size}` : ''}`,
      onclick: () => { state.onlyFavorites = true; haptic(); render(); },
    }),
    state.authorized ? el('button', {
      class: 'chip',
      text: '📦 Заказы',
      onclick: () => { haptic(); openOrders(); },
    }) : null,
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
      onclick: () => openProduct(p),
    }, [
      thumb(p.image, 'thumb'),
      el('div', { class: 'card-body' }, [
        el('div', { class: 'card-title', text: p.title }),
        el('div', { class: 'card-price', text: p.price_text }),
        ratingBadge(p.rating, p.reviews_count, 'rating rating--card'),
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

  const count = state.cart?.lines?.length || 0;
  setMain(count ? `Корзина · ${state.cart.total_text}` : '', count ? () => go('cart') : null);

  // Выдачу поиска и избранное — одной сеткой. Во вкладках находка из второй
  // категории пряталась за первой: на любой запрос человек видел Automatic и решал,
  // что поиск всегда отдаёт одни и те же часы.
  if (state.query.trim() || state.onlyFavorites) {
    return [...head, el('div', { class: 'grid grid--flat' }, cats.flatMap(c => c.products).map(productCard))];
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

function openProduct(p) {
  state.product = p;
  state.gallery = 0;
  go('product');
  loadReviews(p.id);
  loadRelated(`p${p.id}`, `/products/${p.id}/related`);
}

/* Подборка грузится отдельно от экрана и при сбое просто не показывается:
   допродажа не должна мешать купить то, за чем пришли. */
async function loadRelated(key, path) {
  state.related[key] = 'loading';
  try {
    state.related[key] = await api(path);
  } catch {
    state.related[key] = { from_orders: false, products: [] };
  }
  render();
}

function relatedBlock(key, fallbackTitle) {
  const data = state.related[key];
  if (!data || data === 'loading' || !data.products.length) return null;
  const inCart = new Set((state.cart?.lines || []).map(l => l.product_id));
  return el('div', { class: 'related' }, [
    el('div', { class: 'section-title section-title--inner', text: data.from_orders ? 'С этим берут' : fallbackTitle }),
    el('div', { class: 'related-row' }, data.products.map(p => el('div', { class: 'related-card' }, [
      el('div', { class: 'related-open', onclick: () => openProduct(p) }, [
        thumb(p.image, 'related-thumb'),
        el('div', { class: 'related-title', text: p.title }),
        el('div', { class: 'related-price', text: p.price_text }),
      ]),
      state.authorized
        ? el('button', {
            class: `chip related-add${inCart.has(p.id) ? ' chip--on' : ''}`,
            text: inCart.has(p.id) ? 'В корзине' : '+ В корзину',
            disabled: inCart.has(p.id) ? 'disabled' : null,
            onclick: () => addRelated(p.id),
          })
        : null,
    ]))),
  ]);
}

async function addRelated(productId) {
  await patchCart(productId, 1);
  // Корзина изменилась — подборка к ней тоже: добавленное в ней больше не нужно.
  if (state.screen === 'cart') loadRelated('cart', '/cart/related');
}

async function loadReviews(productId) {
  try {
    state.reviews[productId] = await api(`/products/${productId}/reviews`);
  } catch {
    // Отзывы — не повод ломать карточку: без них она остаётся рабочей.
    state.reviews[productId] = [];
  }
  if (state.screen === 'product' && state.product?.id === productId) render();
}

function reviewsBlock(p) {
  const list = state.reviews[p.id];
  const head = el('div', { class: 'section-title section-title--inner' }, [
    el('span', { text: 'Отзывы' }),
    p.reviews_count ? el('span', { class: 'rating', text: `★ ${p.rating.toFixed(1)} · ${p.reviews_count} ${plural(p.reviews_count, 'оценка', 'оценки', 'оценок')}` }) : null,
  ]);
  if (list === undefined) return el('div', { class: 'reviews' }, [head, el('div', { class: 'hint', text: 'Загружаю…' })]);
  if (!list.length) {
    return el('div', { class: 'reviews' }, [head,
      el('div', { class: 'hint', text: 'Отзывов пока нет. Оставить отзыв можно после получения заказа.' })]);
  }
  return el('div', { class: 'reviews' }, [head, ...list.map(r => el('div', { class: 'review' }, [
    el('div', { class: 'review-head' }, [
      el('span', { class: 'review-stars', text: stars(r.rating) }),
      el('span', { class: 'review-author', text: r.author }),
      el('span', { class: 'review-date', text: historyTime(r.created_at).slice(0, 5) }),
    ]),
    r.text ? el('div', { class: 'review-text', text: r.text }) : null,
    r.photo ? el('img', { class: 'review-photo', src: r.photo, loading: 'lazy', alt: '' }) : null,
    r.is_demo ? el('div', { class: 'review-demo', text: 'демо-отзыв' }) : null,
  ]))]);
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
  }
  const addOne = async () => {
    setMain('Добавляю…', null, { progress: true });
    setSecondary('', null);
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
  };
  // Товар уже в корзине — главная кнопка ведёт туда. Раньше она молча клала
  // ещё штуку: человек жал «В корзине · 1 шт», ожидая перехода, и получал две.
  let inlineAdd = null;
  if (state.authorized && !soldOut) {
    if (!line) {
      setMain('Добавить в корзину', addOne);
    } else {
      setMain(`Перейти в корзину · ${line.qty} шт`, () => go('cart'));
      const canAddMore = line.qty < p.stock || p.stock < 0;
      if (canAddMore && !setSecondary('Добавить ещё', addOne)) {
        inlineAdd = el('button', { class: 'chip add-more', text: '+ Добавить ещё', onclick: addOne });
      }
    }
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
      ratingBadge(p.rating, p.reviews_count),
      shareRow(p),
      note ? el('div', { class: `stock${note.low ? ' stock--low' : ''}`, text: note.text }) : null,
      inlineAdd,
      p.description ? el('p', { text: p.description }) : null,
      specs,
      relatedBlock(`p${p.id}`, 'Дополнит покупку'),
      reviewsBlock(p),
      el('div', { class: 'fineprint' }, [
        el('div', { text: 'Доставка по стране 2–5 дней · гарантия 24 месяца' }),
        el('div', { text: 'Витрина-образец: товары вымышленные, деньги не списываются' }),
      ]),
    ]),
  ];
}

/* ---------- поделиться товаром ---------- */

function productLink(p) {
  return state.catalog?.bot ? `https://t.me/${state.catalog.bot}?startapp=p${p.id}` : '';
}

/* Готовое сообщение с фото и кнопкой (Bot API 8.0) сохраняет сервер: только бот
   может положить его на имя покупателя. На старом клиенте, без входа или если
   сервер не смог — обычная ссылка через окно «Поделиться» самого Telegram. */
async function shareProduct(p) {
  haptic();
  if (state.authorized && supports('8.0') && tg?.shareMessage) {
    try {
      const { id } = await api(`/share/${p.id}`, { method: 'POST' });
      tg.shareMessage(id);
      return;
    } catch { /* ниже запасной путь */ }
  }
  const link = productLink(p);
  if (!link) return;
  const url = `https://t.me/share/url?url=${encodeURIComponent(link)}` +
              `&text=${encodeURIComponent(`${p.title} — ${p.price_text}`)}`;
  tg?.openTelegramLink ? tg.openTelegramLink(url) : window.open(url, '_blank');
}

/* Ссылку-виджет в сторис Telegram оставляет только Premium; у остальных
   сторис выйдет с фото и подписью — товар всё равно виден. */
function shareStory(p) {
  haptic();
  const link = productLink(p);
  tg.shareToStory(new URL(p.image, location.origin).href, {
    text: `${p.title} — ${p.price_text}`.slice(0, 200),
    ...(link ? { widget_link: { url: link, name: 'Открыть в магазине' } } : {}),
  });
}

function shareRow(p) {
  // Вне Telegram делиться нечем: ссылка t.me ведёт в клиент, которого тут нет.
  // SDK живёт и в обычном браузере, отличает его только platform 'unknown'.
  if (!tg || tg.platform === 'unknown' || !productLink(p)) return null;
  const story = p.image && supports('7.8') && tg.shareToStory;
  return el('div', { class: 'share-row' }, [
    el('button', { class: 'chip', text: '↗ Поделиться', onclick: () => shareProduct(p) }),
    story ? el('button', { class: 'chip', text: 'В сторис', onclick: () => shareStory(p) }) : null,
  ]);
}

/* t.me/бот?startapp=p<id> из «Поделиться» открывает витрину сразу на товаре.
   Снятый с продажи в каталоге не найдётся — тогда просто главная. */
function startProduct() {
  const param = tg?.initDataUnsafe?.start_param
    || new URLSearchParams(location.search).get('tgWebAppStartParam') || '';
  const m = /^p(\d+)$/.exec(param);
  if (!m) return null;
  const id = Number(m[1]);
  for (const c of state.catalog?.categories || []) {
    const p = c.products.find(x => x.id === id);
    if (p) return p;
  }
  return null;
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

/* Нативный диалог Telegram, если клиент умеет (Bot API 6.2+), иначе браузерный:
   старые десктопы без showConfirm кнопку бы просто проглотили. */
function askConfirm(text) {
  return new Promise(resolve => {
    if (tg?.showConfirm && tg.isVersionAtLeast?.('6.2')) tg.showConfirm(text, resolve);
    else resolve(window.confirm(text));
  });
}

async function clearCart() {
  haptic('medium');
  // Подтверждение обязательно: кнопка стоит рядом со степперами, промах пальцем
  // стирает корзину целиком, а восстановить её нечем.
  if (!await askConfirm('Убрать из корзины все товары?')) return;
  try {
    state.cart = await api('/cart', { method: 'DELETE' });
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
      ratingBadge(l.rating, l.reviews_count, 'rating rating--row'),
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
      el('span', { text: 'Товары' }),
      el('span', { text: cart.total_text }),
    ]),
    cartDeliverySums(cart.total),
    relatedBlock('cart', 'Добавить к заказу'),
    el('div', { class: 'cart-actions' }, [
      el('button', { class: 'chip chip--danger', text: '🗑 Очистить корзину', onclick: clearCart }),
    ]),
  ];
}

/* Сумму с доставкой показываем ещё в корзине, по каждому способу сразу:
   доставка, впервые всплывшая на оформлении, — главная причина бросить
   корзину (Baymard, и то же самое пишут продавцы на r/ecommerce). Способ
   здесь не выбирается, чтобы не плодить второй выбор рядом с чекаутом. */
function cartDeliverySums(itemsTotal) {
  if (!state.deliveryOptions.length) return null;
  return el('div', { class: 'sums sums--cart' }, [
    el('div', { class: 'sums-caption', text: 'Итого с доставкой' }),
    ...state.deliveryOptions.map(o => sumRow(
      o.cost ? `${o.title} · +${o.cost_text}` : `${o.title} · бесплатно`,
      formatMoneyLike(itemsTotal + o.cost),
    )),
  ]);
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

/* Номер одной кнопкой (Bot API 6.9+). Только для входа по initData: у входа через
   бота подписи requestContact нет, а номер без подписи сервер всё равно не примет
   как «из Telegram». Ручной ввод остаётся всегда — делиться номером никто не обязан. */
function canRequestContact() {
  return Boolean(tg?.initData && tg.requestContact && supports('6.9'));
}

function requestPhone() {
  haptic();
  tg.requestContact((shared, event) => {
    const phone = event?.responseUnsafe?.contact?.phone_number;
    if (!shared || !event?.response || !phone) return;  // отказался — остаётся ручной ввод
    state.contact = event.response;
    state.form.phone = phone.startsWith('+') ? phone : `+${phone}`;
    tg.HapticFeedback?.notificationOccurred('success');
    render();
  });
}

function dropContact() {
  state.contact = '';
  state.form.phone = '';
  haptic();
  render();
}

function phoneField(f) {
  if (state.contact) {
    return el('div', { class: 'field' }, [
      el('label', { text: f.label }),
      el('div', { class: 'phone-verified' }, [
        el('span', { text: state.form.phone }),
        el('span', { class: 'phone-badge', text: '✓ из Telegram' }),
        el('button', { class: 'link-btn', text: 'Другой номер', onclick: dropContact }),
      ]),
    ]);
  }
  return null;
}

function screenCheckout() {
  const fields = FIELDS.map(f => {
    if (f.key === 'phone') {
      const verified = phoneField(f);
      if (verified) return verified;
    }
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
      type: f.key === 'phone' ? 'tel' : null,
    });
    const share = f.key === 'phone' && canRequestContact()
      ? el('button', { class: 'chip share-phone', text: '📱 Взять номер из Telegram', onclick: requestPhone })
      : null;
    return el('div', { class: 'field' }, [el('label', { text: f.label }), share, input]);
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
        contact: state.contact,
      }),
    });
    state.contact = '';
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
    // 400 на номере: подпись протухла (час) или не сошлась — дальше вручную.
    if (e.status === 400 && state.contact) { state.contact = ''; state.form.phone = ''; }
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

function reviewForm(o, item) {
  const f = state.reviewForm;
  // Поле текста не перерисовываем на каждый символ: на iOS новый узел
  // закрывает клавиатуру. Ввод пишет в state, render зовут только звёзды.
  const area = el('textarea', {
    class: 'review-input', rows: '3', maxlength: '1000',
    placeholder: 'Что понравилось, что нет — увидят другие покупатели',
  });
  area.value = f.text;
  area.addEventListener('input', () => { f.text = area.value; });
  return el('div', { class: 'review-form' }, [
    el('div', { class: 'review-form-title', text: item.title }),
    el('div', { class: 'star-pick' }, [1, 2, 3, 4, 5].map(n => el('button', {
      class: `star${n <= f.rating ? ' star--on' : ''}`,
      'aria-label': `${n} из 5`,
      text: '★',
      onclick: () => { f.rating = n; haptic(); render(); },
    }))),
    area,
    el('div', { class: 'hint', text: 'Фото можно приложить в чате с ботом: кнопка «Оценить» у заказа.' }),
    el('div', { class: 'order-actions' }, [
      el('button', {
        class: 'chip chip--on', text: 'Отправить', disabled: f.rating ? null : 'disabled',
        onclick: () => sendReview(o.id, item.product_id),
      }),
      el('button', { class: 'chip', text: 'Отмена', onclick: () => { state.reviewForm = null; render(); } }),
    ]),
  ]);
}

function openReview(o, item) {
  haptic();
  state.reviewForm = { order_id: o.id, product_id: item.product_id, rating: item.my_rating || 0, text: '' };
  render();
}

async function sendReview(orderId, productId) {
  const f = state.reviewForm;
  try {
    await api('/reviews', {
      method: 'POST',
      body: JSON.stringify({ order_id: orderId, product_id: productId, rating: f.rating, text: f.text }),
    });
    tg?.HapticFeedback?.notificationOccurred('success');
    state.reviewForm = null;
    state.notice = 'Спасибо! Отзыв появился в карточке товара.';
    // Средняя оценка в каталоге поменялась — перечитываем, как и заказы.
    delete state.reviews[productId];
    await Promise.all([refreshOrders(), reloadCatalog()]);
  } catch (e) {
    if (e.status === 401) { startLogin(); return; }
    state.error = e.message;
  }
  render();
}

async function repeatOrder(id) {
  haptic('medium');
  try {
    const res = await api(`/orders/${id}/repeat`, { method: 'POST' });
    state.cart = res.cart;
    go('cart');
    state.notice = res.skipped.length
      ? `Уже не продаётся или кончилось: ${res.skipped.join(', ')}`
      : 'Положили в корзину по сегодняшним ценам';
  } catch (e) {
    if (e.status === 401) { startLogin(); return; }
    state.error = e.message;
  }
  render();
}

function orderItemLine(o, i) {
  const text = `${i.title} — ${i.qty} шт`;
  if (!o.can_review || !i.product_id) return el('div', { class: 'order-line', text });
  const f = state.reviewForm;
  if (f && f.order_id === o.id && f.product_id === i.product_id) return reviewForm(o, i);
  return el('div', { class: 'order-line order-line--review' }, [
    el('span', { text }),
    el('button', {
      class: 'link-btn',
      text: i.my_rating ? `${stars(i.my_rating)} · изменить` : '★ Оценить',
      onclick: () => openReview(o, i),
    }),
  ]);
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
    ...o.items.map(i => orderItemLine(o, i)),
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
      o.is_final ? el('button', {
        class: 'chip',
        text: '🔁 Повторить',
        onclick: () => repeatOrder(o.id),
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
  // Предлагаем ярлык после заказа, а не на входе: человек только что купил и
  // вернётся за статусом — просьба «добавь на экран» к месту именно сейчас.
  const homeOffer = state.homeScreen === 'missed'
    ? el('div', { class: 'cart-actions' }, [
        el('button', { class: 'chip', text: '📲 Добавить магазин на экран', onclick: addToHome }),
      ])
    : null;
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
    homeOffer,
  ];
}

function addToHome() {
  haptic();
  try { tg.addToHomeScreen(); } catch { /* клиент передумал поддерживать — кнопка просто молчит */ }
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
  // Вторая кнопка нужна только карточке товара: экран сам её поставит, а на
  // остальных она висела бы со старым обработчиком.
  setSecondary('', null);

  if (state.error) {
    app.append(el('div', { class: 'notice notice--bad', text: state.error }));
  }
  if (state.notice) app.append(el('div', { class: 'notice', text: state.notice }));

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
  if (state.ai.open) { setMain('', null); setSecondary('', null); }

  // Своего хедера с крестиком нет: назад — встроенная кнопка Telegram.
  const back = tg?.BackButton;
  if (back) (state.screen === 'catalog' || state.screen === 'done') && !state.ai.open ? back.hide() : back.show();

  // Свайп вниз на оформлении закрывал витрину вместе с набранным адресом.
  // Только здесь: в каталоге переспрашивать «точно закрыть?» — раздражать.
  if (supports('6.2')) {
    state.screen === 'checkout' ? tg.enableClosingConfirmation() : tg.disableClosingConfirmation();
  }
}

/* Полный экран — только на телефонах: на десктопе он разворачивает окно
   поверх всего, а выигрыша в площади там нет. Верх экрана в этом режиме
   накрывает шапка Telegram, отступ под неё ставит класс .fullscreen. */
function syncFullscreen() {
  document.documentElement.classList.toggle('fullscreen', Boolean(tg?.isFullscreen));
}

function setupTelegramOnce() {
  if (!tg || setupTelegramOnce.done) return;
  // boot() вызывается повторно после входа через бота; подписки, навешанные
  // дважды, срабатывали дважды — «назад» перескакивал через экран.
  setupTelegramOnce.done = true;
  tg.ready();
  tg.expand();
  tg.BackButton?.onClick(goBack);
  tg.onEvent?.('themeChanged', () => render());
  if (!supports('8.0')) return;
  tg.onEvent('fullscreenChanged', syncFullscreen);
  tg.onEvent('homeScreenAdded', () => {
    state.homeScreen = 'added';
    if (state.screen === 'done') render();
  });
  try { tg.checkHomeScreenStatus(status => { state.homeScreen = status; }); } catch { /* нет так нет */ }
  if (['ios', 'android'].includes(tg.platform)) {
    try { tg.requestFullscreen(); } catch { /* fullscreenFailed — остаёмся в обычном окне */ }
  }
  syncFullscreen();
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
    rememberPending(code);

    screenLoginWaiting();
    tg?.openTelegramLink ? tg.openTelegramLink(link) : window.open(link, '_blank');

    let attempts = 0;
    pollTimer = setInterval(async () => {
      attempts += 1;
      if (attempts > 100) { clearInterval(pollTimer); return; }
      try {
        const r = await fetch(`/api/auth/poll?code=${encodeURIComponent(code)}`);
        if (!r.ok) { clearInterval(pollTimer); rememberPending(null); screenLocked('Код входа устарел'); return; }
        const data = await r.json();
        if (data.ready && data.token) {
          clearInterval(pollTimer);
          rememberPending(null);
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
  setupTelegramOnce();
  state.session = savedSession();
  // С подписью Telegram вход не нужен — незачем и дёргать сервер.
  if (!tg?.initData && !state.session) await pickUpPendingLogin();

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
  const shared = startProduct();
  if (shared) { openProduct(shared); return; }
  render();
}

boot();
