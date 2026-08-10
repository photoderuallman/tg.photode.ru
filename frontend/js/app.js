const app = document.querySelector("#app");
const unlockScreen = document.querySelector("#unlock-screen");
const unlockForm = document.querySelector("#unlock-form");
const accessKeyInput = document.querySelector("#access-key");
const accessError = document.querySelector("#access-error");
const mainScreen = document.querySelector("#main-screen");
const chatScreen = document.querySelector("#chat-screen");
const chatList = document.querySelector("#chat-list");
const chatBack = document.querySelector("#chat-back");
const accountName = document.querySelector(".main-header .header-name");
const accountBowl = document.querySelector(".main-header .profile-bowl");
const connectionStatus = document.querySelector("#connection-status");
const chatName = document.querySelector("#chat-name");
const chatBowl = document.querySelector("#chat-bowl");
const contactStatus = document.querySelector("#contact-status");
const messageScroller = document.querySelector("#message-scroller");
const messageThread = document.querySelector("#message-thread");
const composer = document.querySelector("#composer");
const messageInput = document.querySelector("#message-input");
const actionControl = document.querySelector("#action-control");
const mediaControl = document.querySelector("#media-control");
const mediaInput = document.querySelector("#media-input");
const recordingLayer = document.querySelector("#recording-layer");
const recordingPreview = document.querySelector("#recording-preview");
const recordingCanvas = document.querySelector("#recording-canvas");
const recordingTimer = document.querySelector("#recording-timer");
const recordingCancel = document.querySelector("#recording-cancel");
const recordingSend = document.querySelector("#recording-send");
const recordingSwitch = document.querySelector("#recording-switch");

const IS_PHOTODE_HOST = /(^|\.)photode\.ru$/i.test(window.location.hostname);
const SCRIPT_URL = new URL(document.currentScript?.src || window.location.href);
const RELAY_ENDPOINT = IS_PHOTODE_HOST ? new URL("../../api/index.php", SCRIPT_URL) : null;
const API_BASE_URL = new URL("/api/", window.location.origin);
const SESSION_STORAGE_KEY = "tg.photode.web-session.v1";
const HOLD_DELAY_MS = 500;
const VIDEO_NOTE_LIMIT_MS = 60_000;

let accessToken = window.localStorage.getItem(SESSION_STORAGE_KEY) || "";
let account = null;
let chats = [];
let selectedChat = null;
let messages = [];
let currentPresence = null;
let eventSocket = null;
let eventRequestController = null;
let eventGeneration = 0;
let reconnectTimer = null;
let reconnectAttempt = 0;
let snapshotRefreshTimer = null;
let snapshotRefreshInFlight = false;
let chatActionTimer = null;
let typingCancelTimer = null;
let lastTypingSentAt = 0;
let readObserver = null;
let pendingReadIds = new Set();
let readFlushTimer = null;
let holdTimer = null;
let ignoreNextActionClick = false;
let recording = false;
let recordingStartedAt = 0;
let recordingFrame = null;
let recordingDrawFrame = null;
let cameraStream = null;
let recorderStream = null;
let mediaRecorder = null;
let recorderResult = null;
let recorderChunks = [];
let facingMode = "user";
let objectUrls = new Set();
let mediaUrlPromises = new Map();
let accentColorPromises = new Map();
let chatLoadGeneration = 0;
let renderedMessageIds = new Set();
let optimisticMessageId = -Date.now();
let videoNoteUploadQueue = Promise.resolve();
let scrollIntentVersion = 0;
let threadRenderVersion = 0;
let scrollCommandVersion = 0;

class ApiError extends Error {
  constructor(message, status = 0, code = "request_failed") {
    super(message);
    this.status = status;
    this.code = code;
  }
}

function apiUrl(path) {
  if (!RELAY_ENDPOINT) {
    return new URL(path.replace(/^\/api\/?/, ""), API_BASE_URL).toString();
  }
  const upstreamPath = new URL(path, "https://telegram.invalid");
  const relayUrl = new URL(RELAY_ENDPOINT);
  relayUrl.searchParams.set("_path", upstreamPath.pathname);
  upstreamPath.searchParams.forEach((value, key) => relayUrl.searchParams.append(key, value));
  return relayUrl.toString();
}

async function apiRequest(path, options = {}) {
  const { auth = true, raw = false, ...requestOptions } = options;
  const headers = new Headers(requestOptions.headers || {});
  if (auth && accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  const response = await fetch(apiUrl(path), { ...requestOptions, headers, mode: "same-origin" });

  if (response.status === 401 && auth) {
    accessToken = "";
    window.localStorage.removeItem(SESSION_STORAGE_KEY);
    showUnlock("Open the private link again to unlock this device.");
    throw new ApiError("Private session required.", 401, "web_session_required");
  }

  if (!response.ok) {
    let detail = null;
    try {
      detail = (await response.json()).detail;
    } catch {
      detail = null;
    }
    throw new ApiError(
      detail?.message || `Request failed with ${response.status}.`,
      response.status,
      detail?.code || "request_failed",
    );
  }

  if (raw) return response;
  if (response.status === 204) return null;
  return response.json();
}

async function exchangeAccessKey(key) {
  const session = await apiRequest("/api/session", {
    auth: false,
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ access_key: key }),
  });
  accessToken = session.token;
  window.localStorage.setItem(SESSION_STORAGE_KEY, accessToken);
}

async function consumeAccessLink() {
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  const key = fragment.get("access");
  if (!key) return false;
  window.history.replaceState(window.history.state, "", `${window.location.pathname}${window.location.search}`);
  await exchangeAccessKey(key);
  return true;
}

function showUnlock(message = "") {
  closeEventSocket();
  app.hidden = true;
  unlockScreen.hidden = false;
  accessError.value = message;
  window.setTimeout(() => accessKeyInput.focus(), 0);
}

function showApp() {
  unlockScreen.hidden = true;
  app.hidden = false;
  mainScreen.hidden = false;
  chatScreen.hidden = false;
  syncVisualViewport();
  requestAnimationFrame(() => app.classList.add("is-motion-ready"));
}

function setConnectionState(state) {
  setAnimatedLabel(connectionStatus, state);
  if (selectedChat && state !== "RDY") setContactStatus(state);
}

function setContactStatus(status) {
  setAnimatedLabel(contactStatus, status);
  contactStatus.classList.toggle("is-typing", status === "TYP");
  chatBack.classList.toggle("is-contact-active", status === "ONL" || status === "TYP");
}

function setAnimatedLabel(element, value) {
  if (element.textContent === value) return;
  element.textContent = value;
  element.classList.remove("is-status-changing");
  void element.offsetWidth;
  element.classList.add("is-status-changing");
  const finish = (event) => {
    if (event.animationName !== "status-switch") return;
    element.classList.remove("is-status-changing");
    element.removeEventListener("animationend", finish);
  };
  element.addEventListener("animationend", finish);
}

function colorsFor(value) {
  let hash = 2166136261;
  for (const character of String(value)) {
    hash ^= character.codePointAt(0);
    hash = Math.imul(hash, 16777619);
  }
  const hueA = Math.abs(hash) % 360;
  const hueB = (hueA + 52 + (Math.abs(hash >> 8) % 80)) % 360;
  return [`hsl(${hueA} 46% 58%)`, `hsl(${hueB} 48% 18%)`];
}

function setBowlColors(element, colors) {
  element.style.setProperty("--bowl-a", colors[0]);
  element.style.setProperty("--bowl-b", colors[1]);
}

function chatTitle(chat) {
  return chat.is_saved_messages || chat.peer_user_id === account?.id
    ? "Saved Messages"
    : chat.title;
}

async function setTelegramBowlColors(element, subject, fallbackValue) {
  setBowlColors(element, colorsFor(fallbackValue));
  const fileId = subject?.profile_photo_file_id;
  const downloadUrl = subject?.profile_photo_url;
  if (!fileId || !downloadUrl) return;
  if (!accentColorPromises.has(fileId)) {
    accentColorPromises.set(fileId, (async () => {
      const url = await mediaBlobUrl({ file_id: fileId, download_url: downloadUrl });
      return extractAccentColors(url, fallbackValue);
    })());
  }
  try {
    const colors = await accentColorPromises.get(fileId);
    if (element.isConnected) setBowlColors(element, colors);
  } catch {
    accentColorPromises.delete(fileId);
  }
}

async function extractAccentColors(url, fallbackValue) {
  const image = new Image();
  image.decoding = "async";
  image.src = url;
  await image.decode();
  const canvas = document.createElement("canvas");
  canvas.width = 32;
  canvas.height = 32;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  context.drawImage(image, 0, 0, canvas.width, canvas.height);
  const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
  const buckets = new Map();
  for (let index = 0; index < pixels.length; index += 4) {
    if (pixels[index + 3] < 180) continue;
    const red = pixels[index];
    const green = pixels[index + 1];
    const blue = pixels[index + 2];
    const maximum = Math.max(red, green, blue);
    const minimum = Math.min(red, green, blue);
    const chroma = maximum - minimum;
    const luminance = (maximum + minimum) / 510;
    if (luminance < 0.08 || luminance > 0.94) continue;
    const key = `${red >> 4}:${green >> 4}:${blue >> 4}`;
    const current = buckets.get(key) || { red: 0, green: 0, blue: 0, weight: 0 };
    const weight = 1 + chroma / 96;
    current.red += red * weight;
    current.green += green * weight;
    current.blue += blue * weight;
    current.weight += weight;
    buckets.set(key, current);
  }
  const candidates = [...buckets.values()].map((bucket) => ({
    red: bucket.red / bucket.weight,
    green: bucket.green / bucket.weight,
    blue: bucket.blue / bucket.weight,
    weight: bucket.weight,
  })).sort((left, right) => right.weight - left.weight);
  if (!candidates.length) return colorsFor(fallbackValue);
  const first = candidates[0];
  const second = candidates.slice(1).reduce((best, candidate) => {
    const distance = Math.hypot(
      candidate.red - first.red,
      candidate.green - first.green,
      candidate.blue - first.blue,
    );
    const score = distance * Math.sqrt(candidate.weight);
    return !best || score > best.score ? { candidate, score } : best;
  }, null)?.candidate || first;
  const firstHsl = rgbToHsl(first.red, first.green, first.blue);
  const secondHsl = rgbToHsl(second.red, second.green, second.blue);
  return [
    `hsl(${Math.round(firstHsl.hue)} ${Math.round(Math.max(42, firstHsl.saturation))}% 58%)`,
    `hsl(${Math.round(secondHsl.hue)} ${Math.round(Math.max(38, secondHsl.saturation))}% 20%)`,
  ];
}

function rgbToHsl(red, green, blue) {
  const r = red / 255;
  const g = green / 255;
  const b = blue / 255;
  const maximum = Math.max(r, g, b);
  const minimum = Math.min(r, g, b);
  const delta = maximum - minimum;
  let hue = 0;
  if (delta) {
    if (maximum === r) hue = 60 * (((g - b) / delta) % 6);
    else if (maximum === g) hue = 60 * ((b - r) / delta + 2);
    else hue = 60 * ((r - g) / delta + 4);
  }
  if (hue < 0) hue += 360;
  const lightness = (maximum + minimum) / 2;
  const saturation = delta ? delta / (1 - Math.abs(2 * lightness - 1)) : 0;
  return { hue, saturation: saturation * 100 };
}

function chatIsChecked(chat) {
  return Boolean(
    chat.last_message_is_outgoing
      && chat.last_message_id
      && chat.last_message_id <= chat.last_read_outbox_message_id,
  );
}

function renderChatList() {
  chatList.replaceChildren();
  chats.forEach((chat) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "chat-row";
    row.dataset.chatId = String(chat.id);
    row.setAttribute("aria-label", `Open chat with ${chatTitle(chat)}`);

    const bowl = document.createElement("span");
    bowl.className = "chat-bowl";
    bowl.setAttribute("aria-hidden", "true");
    void setTelegramBowlColors(bowl, chat, chat.id);

    const origin = document.createElement("span");
    origin.className = `message-origin${chatIsChecked(chat) ? " is-checked" : ""}`;
    origin.textContent = chat.last_message_is_outgoing ? "Y." : "T.";

    const copy = document.createElement("span");
    copy.className = "chat-row-copy";
    const name = document.createElement("span");
    name.className = "chat-row-name";
    name.textContent = chatTitle(chat);
    const preview = document.createElement("span");
    preview.className = "chat-row-preview";
    preview.textContent = chat.last_message || "";
    copy.append(name, preview);
    row.append(bowl, origin, copy);
    row.addEventListener("click", () => void openChat(chat, { push: true }));
    chatList.append(row);
  });
}

async function loadChats() {
  setConnectionState("UPD");
  chats = await apiRequest("/api/chats?limit=50");
  renderChatList();
  setConnectionState("RDY");
}

function showScreen(name) {
  const isMain = name === "main";
  app.dataset.screen = name;
  mainScreen.hidden = false;
  chatScreen.hidden = false;
  mainScreen.setAttribute("aria-hidden", String(!isMain));
  chatScreen.setAttribute("aria-hidden", String(isMain));
  mainScreen.classList.toggle("is-active", isMain);
  chatScreen.classList.toggle("is-active", !isMain);
}

function updateUrl(view, chat = null) {
  const url = new URL(window.location.href);
  url.searchParams.set("view", view);
  if (chat) url.searchParams.set("chat", String(chat.id));
  else url.searchParams.delete("chat");
  window.history.pushState({ view, chat: chat?.id || null }, "", url);
}

async function showMain({ push = false, refresh = true } = {}) {
  chatLoadGeneration += 1;
  await stopRecording(false);
  collapseFocusedVideo();
  selectedChat = null;
  currentPresence = null;
  renderedMessageIds.clear();
  showScreen("main");
  if (push) updateUrl("main");
  if (refresh) {
    try {
      await loadChats();
    } catch (error) {
      if (error.status !== 401) setConnectionState("CNT");
    }
  }
  connectEvents();
}

async function openChat(chat, { push = false } = {}) {
  const generation = ++chatLoadGeneration;
  selectedChat = chat;
  messages = [];
  renderedMessageIds.clear();
  chatName.textContent = chatTitle(chat);
  void setTelegramBowlColors(chatBowl, chat, chat.id);
  setContactStatus("UPD");
  showScreen("chat");
  if (push) updateUrl("chat", chat);

  const historyRequest = apiRequest(`/api/chats/${chat.id}/messages?limit=50`);
  const profileRequest = chat.peer_user_id
    ? apiRequest(`/api/users/${chat.peer_user_id}`).catch(() => null)
    : Promise.resolve(null);
  try {
    const history = await historyRequest;
    if (generation !== chatLoadGeneration || selectedChat?.id !== chat.id) return;
    messages = history.sort(compareMessages);
    renderThread({ scroll: true, animateNew: false });
    connectEvents(chat.id);
    const profile = await profileRequest;
    if (generation !== chatLoadGeneration || selectedChat?.id !== chat.id) return;
    currentPresence = profile?.presence || null;
    if (profile) void setTelegramBowlColors(chatBowl, profile, chat.id);
    restorePresence();
  } catch (error) {
    if (error.status !== 401) setContactStatus("CNT");
  }
}

function compareMessages(left, right) {
  const timeDifference = Date.parse(left.sent_at) - Date.parse(right.sent_at);
  return timeDifference || left.id - right.id;
}

function messageStatus(message) {
  if (!message.is_outgoing) return "T.";
  if (message.kind === "video_note" && message.media?.is_opened) return "Y+C";
  return "Y.";
}

function messageIsRead(message) {
  if (!message.is_outgoing) return Boolean(message.is_read);
  if (message.is_read) return true;
  const marker = selectedChat?.last_read_outbox_message_id || 0;
  return Boolean(message.id > 0 && marker && message.id <= marker);
}

function createOrigin(message) {
  const origin = document.createElement("span");
  origin.className = `message-origin${message.is_outgoing && !messageIsRead(message) ? " is-unread" : " is-read"}`;
  origin.textContent = messageStatus(message);
  return origin;
}

function createTextGroup(group, { entering = false } = {}) {
  const article = document.createElement("article");
  const unchecked = group[0].is_outgoing && !messageIsRead(group[0]);
  article.className = `message-group${unchecked ? " is-unchecked" : ""}${entering ? " is-entering" : ""}`;
  article.dataset.messageIds = group.map((message) => message.id).join(",");
  article.append(createOrigin(group[0]));

  const copy = document.createElement("div");
  copy.className = "message-copy";
  group.forEach((message) => {
    const line = document.createElement("p");
    line.className = "message-line";
    line.dataset.messageId = String(message.id);
    appendFormattedText(line, message.text || "[Unsupported message]", message.entities || []);
    copy.append(line);
  });
  article.append(copy);
  attachIncomingReadIds(article, group);
  return article;
}

function appendFormattedText(container, text, entities) {
  const customEntities = entities
    .filter((entity) => entity.type === "custom_emoji" && entity.custom_emoji_id)
    .sort((left, right) => left.offset - right.offset);
  if (!customEntities.length) {
    container.textContent = text;
    return;
  }

  let cursor = 0;
  customEntities.forEach((entity) => {
    if (entity.offset < cursor || entity.offset > text.length) return;
    container.append(document.createTextNode(text.slice(cursor, entity.offset)));
    const fallback = text.slice(entity.offset, entity.offset + entity.length);
    const placeholder = document.createElement("span");
    placeholder.textContent = fallback;
    container.append(placeholder);
    void loadCustomEmoji(placeholder, entity.custom_emoji_id, fallback);
    cursor = entity.offset + entity.length;
  });
  container.append(document.createTextNode(text.slice(cursor)));
}

async function loadCustomEmoji(placeholder, customEmojiId, fallback) {
  try {
    const emoji = await apiRequest(`/api/emojis/custom/${customEmojiId}`);
    if (!new Set(["webp", "webm"]).has(emoji.format)) return;
    const url = await mediaBlobUrl({ file_id: emoji.file_id, download_url: emoji.download_url });
    const element = document.createElement(emoji.format === "webm" ? "video" : "img");
    element.className = "custom-emoji";
    element.src = url;
    if (element instanceof HTMLVideoElement) {
      element.autoplay = true;
      element.loop = true;
      element.muted = true;
      element.playsInline = true;
    } else {
      element.alt = fallback;
    }
    placeholder.replaceWith(element);
  } catch {
    placeholder.textContent = fallback;
  }
}

function createMediaGroup(message, { entering = false } = {}) {
  const article = document.createElement("article");
  const unchecked = message.is_outgoing && !messageIsRead(message);
  article.className = `message-group gallery-media${unchecked ? " is-unchecked" : ""}${entering ? " is-entering" : ""}`;
  article.dataset.messageId = String(message.id);
  article.dataset.messageIds = String(message.id);
  article.append(createOrigin(message));

  if (message.kind === "video_note" && message.media) {
    article.classList.add("video-message");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "video-note is-background-playing is-loading";
    button.setAttribute("aria-label", "Play video message with sound");
    const video = document.createElement("video");
    video.muted = true;
    video.loop = true;
    video.autoplay = true;
    video.playsInline = true;
    video.preload = "metadata";
    button.append(video);
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      void toggleVideoNote(button, video, message);
    });
    video.addEventListener("ended", () => collapseFocusedVideo());
    article.append(button);
    void loadMediaElement(video, message.media).then(() => {
      button.classList.remove("is-loading");
      void video.play().catch(() => {});
    });
  } else if (message.media) {
    let element;
    if (message.kind === "photo") {
      element = document.createElement("img");
      element.alt = message.text || "Telegram photo";
      element.addEventListener("click", () => void markContentOpened(message));
    } else if (message.kind === "voice_note") {
      element = document.createElement("audio");
      element.controls = true;
      element.preload = "metadata";
      element.addEventListener("play", () => void markContentOpened(message));
    } else {
      element = document.createElement("video");
      element.controls = true;
      element.playsInline = true;
      element.preload = "metadata";
      element.addEventListener("play", () => void markContentOpened(message));
    }
    reserveMediaGeometry(element, message.media);
    element.classList.add("is-media-loading");
    article.append(element);
    void loadMediaElement(element, message.media).finally(() => element.classList.remove("is-media-loading"));
    if (message.text) {
      const caption = document.createElement("p");
      caption.className = "message-line";
      appendFormattedText(caption, message.text, message.entities || []);
      article.append(caption);
    }
  } else {
    const copy = document.createElement("div");
    copy.className = "message-copy";
    copy.textContent = message.text || "[Unsupported message]";
    article.append(copy);
  }
  attachIncomingReadIds(article, [message]);
  return article;
}

async function loadMediaElement(element, media) {
  const anchor = captureScrollAnchor();
  try {
    element.src = await mediaBlobUrl(media);
    await waitForMediaGeometry(element);
    const restored = restoreScrollAnchor(anchor);
    if (
      !restored
      && anchor.intentVersion === scrollIntentVersion
      && anchor.scrollCommandVersion !== scrollCommandVersion
    ) scrollThreadToBottom();
  } catch {
    element.removeAttribute("src");
  }
}

function waitForMediaGeometry(element) {
  const isImage = element instanceof HTMLImageElement;
  const ready = isImage ? element.complete : element.readyState >= HTMLMediaElement.HAVE_METADATA;
  if (ready) return Promise.resolve();
  return new Promise((resolve) => {
    const eventName = isImage ? "load" : "loadedmetadata";
    const finish = () => {
      window.clearTimeout(timeout);
      element.removeEventListener(eventName, finish);
      element.removeEventListener("error", finish);
      resolve();
    };
    const timeout = window.setTimeout(finish, 5000);
    element.addEventListener(eventName, finish, { once: true });
    element.addEventListener("error", finish, { once: true });
  });
}

function reserveMediaGeometry(element, media) {
  if (media.width && media.height) {
    element.style.aspectRatio = `${media.width} / ${media.height}`;
  }
}

function mediaBlobUrl(media) {
  if (media.local_url) return Promise.resolve(media.local_url);
  if (!mediaUrlPromises.has(media.file_id)) {
    mediaUrlPromises.set(media.file_id, (async () => {
      const response = await apiRequest(media.download_url, { raw: true });
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      objectUrls.add(url);
      return url;
    })());
  }
  return mediaUrlPromises.get(media.file_id);
}

function attachIncomingReadIds(article, group) {
  const incomingIds = group.filter((message) => !message.is_outgoing && !message.is_read).map((message) => message.id);
  if (incomingIds.length) article.dataset.incomingIds = incomingIds.join(",");
}

function renderThread({ scroll = false, animateNew = false } = {}) {
  const anchor = captureScrollAnchor();
  anchor.renderVersion = ++threadRenderVersion;
  const previousScrollTop = messageScroller.scrollTop;
  const previousHeight = messageScroller.scrollHeight;
  const wasNearBottom = previousHeight - previousScrollTop - messageScroller.clientHeight < 80;
  readObserver?.disconnect();
  messageThread.replaceChildren();
  const groups = [];
  messages.forEach((message) => {
    const previous = groups.at(-1);
    const canJoin = message.kind === "text"
      && Array.isArray(previous)
      && previous.at(-1).kind === "text"
      && previous.at(-1).is_outgoing === message.is_outgoing
      && (!message.is_outgoing || messageIsRead(previous.at(-1)) === messageIsRead(message));
    if (canJoin) previous.push(message);
    else groups.push(message.kind === "text" ? [message] : message);
  });
  const nextRenderedIds = new Set();
  groups.forEach((group) => {
    const groupMessages = Array.isArray(group) ? group : [group];
    groupMessages.forEach((message) => nextRenderedIds.add(String(message.id)));
    const entering = animateNew && groupMessages.some((message) => !renderedMessageIds.has(String(message.id)));
    messageThread.append(
      Array.isArray(group)
        ? createTextGroup(group, { entering })
        : createMediaGroup(group, { entering }),
    );
  });
  renderedMessageIds = nextRenderedIds;
  observeVisibleMessages();
  requestAnimationFrame(() => {
    if (scroll || (animateNew && wasNearBottom)) scrollThreadToBottom({ smooth: animateNew });
    else if (!restoreScrollAnchor(anchor)) messageScroller.scrollTop = previousScrollTop;
  });
}

function captureScrollAnchor() {
  const scrollerRect = messageScroller.getBoundingClientRect();
  const candidate = [...messageThread.querySelectorAll("[data-message-id]")].find((element) => {
    const rect = element.getBoundingClientRect();
    return rect.bottom > scrollerRect.top && rect.top < scrollerRect.bottom;
  });
  return {
    messageId: candidate?.dataset.messageId || null,
    offset: candidate ? candidate.getBoundingClientRect().top - scrollerRect.top : 0,
    scrollTop: messageScroller.scrollTop,
    intentVersion: scrollIntentVersion,
    renderVersion: threadRenderVersion,
    scrollCommandVersion,
  };
}

function restoreScrollAnchor(anchor) {
  if (
    !anchor
    || anchor.intentVersion !== scrollIntentVersion
    || anchor.renderVersion !== threadRenderVersion
    || anchor.scrollCommandVersion !== scrollCommandVersion
  ) return false;
  const candidate = [...messageThread.querySelectorAll("[data-message-id]")]
    .find((element) => element.dataset.messageId === anchor.messageId);
  if (!candidate) {
    messageScroller.scrollTop = anchor.scrollTop;
    return false;
  }
  const scrollerTop = messageScroller.getBoundingClientRect().top;
  const currentOffset = candidate.getBoundingClientRect().top - scrollerTop;
  messageScroller.scrollTop += currentOffset - anchor.offset;
  return true;
}

function observeVisibleMessages() {
  readObserver = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting || entry.intersectionRatio < 0.5) return;
      const ids = (entry.target.dataset.incomingIds || "").split(",").filter(Boolean).map(Number);
      ids.forEach((id) => pendingReadIds.add(id));
    });
    scheduleReadFlush();
  }, { root: messageScroller, threshold: 0.5 });
  messageThread.querySelectorAll("[data-incoming-ids]").forEach((element) => readObserver.observe(element));
}

function scheduleReadFlush() {
  window.clearTimeout(readFlushTimer);
  readFlushTimer = window.setTimeout(() => void flushReads(), 350);
}

async function flushReads() {
  if (!selectedChat || !pendingReadIds.size) return;
  const ids = [...pendingReadIds];
  pendingReadIds.clear();
  try {
    await apiRequest(`/api/chats/${selectedChat.id}/read`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message_ids: ids }),
    });
    messages.forEach((message) => {
      if (ids.includes(message.id)) message.is_read = true;
    });
  } catch {
    ids.forEach((id) => pendingReadIds.add(id));
  }
}

function scrollThreadToBottom({ smooth = false } = {}) {
  scrollCommandVersion += 1;
  messageScroller.scrollTo({
    top: messageScroller.scrollHeight,
    behavior: smooth ? "smooth" : "auto",
  });
}

function isThreadNearBottom() {
  return messageScroller.scrollHeight - messageScroller.scrollTop - messageScroller.clientHeight < 80;
}

function formatPresence(presence) {
  if (!presence) return selectedChat?.type === "private" ? "LSR" : "RDY";
  if (presence.state === "online") return "ONL";
  if (presence.state === "recently") return "LSR";
  if (presence.last_seen_at) {
    const date = new Date(presence.last_seen_at);
    return `${String(date.getHours()).padStart(2, "0")}.${String(date.getMinutes()).padStart(2, "0")}`;
  }
  return "LSR";
}

function restorePresence() {
  setContactStatus(formatPresence(currentPresence));
}

function closeEventSocket() {
  eventGeneration += 1;
  window.clearTimeout(reconnectTimer);
  reconnectTimer = null;
  window.clearInterval(snapshotRefreshTimer);
  snapshotRefreshTimer = null;
  eventRequestController?.abort();
  eventRequestController = null;
  if (eventSocket) {
    eventSocket.onclose = null;
    eventSocket.close();
    eventSocket = null;
  }
}

function connectEvents(chatId = null) {
  closeEventSocket();
  if (IS_PHOTODE_HOST) {
    connectHttpEvents(chatId);
    return;
  }
  const url = new URL(apiUrl("/api/events"));
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  if (chatId !== null) url.searchParams.set("chat_id", String(chatId));
  try {
    eventSocket = accessToken
      ? new WebSocket(url, ["tg-session", accessToken])
      : new WebSocket(url);
  } catch {
    scheduleReconnect(chatId);
    return;
  }
  const socket = eventSocket;
  setConnectionState("CNT");
  socket.addEventListener("open", () => {
    if (eventSocket !== socket) return;
    reconnectAttempt = 0;
    setConnectionState("RDY");
    if (selectedChat) restorePresence();
  });
  socket.addEventListener("message", (event) => {
    if (eventSocket === socket) handleTelegramEvent(JSON.parse(event.data));
  });
  socket.addEventListener("close", (event) => {
    if (eventSocket !== socket) return;
    if (event.code === 4401) {
      showUnlock("Open the private link again to unlock this device.");
      return;
    }
    scheduleReconnect(chatId);
  });
  socket.addEventListener("error", () => {
    if (eventSocket === socket) setConnectionState("CNT");
  });
}

function connectHttpEvents(chatId) {
  const generation = eventGeneration;
  reconnectAttempt = 0;
  setConnectionState("RDY");
  if (selectedChat) restorePresence();
  snapshotRefreshTimer = window.setInterval(() => void refreshRelaySnapshot(), 4000);
  void pollHttpEvents(chatId, generation);
}

async function pollHttpEvents(chatId, generation) {
  while (generation === eventGeneration && accessToken) {
    const query = new URLSearchParams({ timeout_seconds: "20" });
    if (chatId !== null) query.set("chat_id", String(chatId));
    eventRequestController = new AbortController();
    try {
      const response = await apiRequest(`/api/events/next?${query}`, {
        raw: true,
        signal: eventRequestController.signal,
        cache: "no-store",
      });
      if (generation !== eventGeneration) return;
      reconnectAttempt = 0;
      setConnectionState("RDY");
      if (selectedChat) restorePresence();
      if (response.status !== 204) handleTelegramEvent(await response.json());
    } catch (error) {
      if (generation !== eventGeneration || error.name === "AbortError" || error.status === 401) return;
      setConnectionState(reconnectAttempt ? "UPD" : "CNT");
      const delay = Math.min(1000 * 2 ** reconnectAttempt, 15_000);
      reconnectAttempt += 1;
      await new Promise((resolve) => window.setTimeout(resolve, delay));
    }
  }
}

async function refreshRelaySnapshot() {
  if (snapshotRefreshInFlight || document.hidden || !accessToken) return;
  snapshotRefreshInFlight = true;
  const chat = selectedChat;
  try {
    if (!chat) {
      chats = await apiRequest("/api/chats?limit=50");
      renderChatList();
    } else {
      const requests = [apiRequest(`/api/chats/${chat.id}/messages?limit=50`)];
      if (chat.peer_user_id) requests.push(apiRequest(`/api/users/${chat.peer_user_id}`));
      const [history, profile] = await Promise.all(requests);
      if (selectedChat?.id !== chat.id) return;
      const nextMessages = history.sort(compareMessages);
      const changed = JSON.stringify(nextMessages) !== JSON.stringify(messages);
      currentPresence = profile?.presence || currentPresence;
      if (changed) {
        const wasNearBottom = isThreadNearBottom();
        messages = nextMessages;
        renderThread({ scroll: wasNearBottom });
      }
      restorePresence();
    }
    setConnectionState("RDY");
  } catch (error) {
    if (error.status !== 401) setConnectionState("UPD");
  } finally {
    snapshotRefreshInFlight = false;
  }
}

function scheduleReconnect(chatId) {
  setConnectionState(reconnectAttempt ? "UPD" : "CNT");
  const delay = Math.min(1000 * 2 ** reconnectAttempt, 15_000);
  reconnectAttempt += 1;
  window.clearTimeout(reconnectTimer);
  reconnectTimer = window.setTimeout(() => connectEvents(chatId), delay);
}

function handleTelegramEvent(event) {
  if (["message.new", "message.sent", "message.failed"].includes(event.type) && event.message) {
    if (selectedChat && event.chat_id === selectedChat.id) {
      upsertMessage(event.message, event.old_message_id);
      renderThread({ animateNew: true });
    } else {
      void loadChats().catch(() => setConnectionState("UPD"));
    }
    return;
  }
  if (!selectedChat || event.chat_id !== selectedChat.id) return;
  if (event.type === "presence.updated" && event.presence) {
    currentPresence = event.presence;
    restorePresence();
  } else if (event.type === "chat.action" && event.action?.sender_id !== account?.id) {
    window.clearTimeout(chatActionTimer);
    if (event.action.action === "typing") {
      setContactStatus("TYP");
      chatActionTimer = window.setTimeout(restorePresence, 4500);
    } else if (event.action.action === "cancel") {
      restorePresence();
    }
  } else if (event.type === "receipt.updated" && event.receipt) {
    applyReceipt(event.receipt);
  } else if (event.type === "message.content_opened" && event.message_id) {
    const message = messages.find((item) => item.id === event.message_id);
    if (message?.is_outgoing && message.media) {
      message.media.is_opened = true;
      renderThread();
    }
  } else if (event.type === "message.content_updated" && event.message_id) {
    const message = messages.find((item) => item.id === event.message_id);
    if (message) {
      message.kind = event.kind || message.kind;
      message.text = event.text ?? message.text;
      message.entities = event.entities || message.entities;
      message.media = event.media || message.media;
      renderThread();
    }
  }
}

function upsertMessage(message, oldMessageId = null) {
  let index = messages.findIndex((item) => item.id === message.id || (oldMessageId && item.id === oldMessageId));
  if (index < 0 && message.is_outgoing) {
    index = messages.findIndex((item) => item.client_pending && item.kind === message.kind);
  }
  if (index >= 0) messages[index] = message;
  else messages.push(message);
  messages.sort(compareMessages);
}

function replaceOptimisticMessage(localId, message) {
  messages = messages.filter((item) => item.id !== localId && item.id !== message.id);
  messages.push(message);
  messages.sort(compareMessages);
}

function applyReceipt(receipt) {
  if (receipt.direction === "outbox" && selectedChat) {
    selectedChat.last_read_outbox_message_id = Math.max(
      selectedChat.last_read_outbox_message_id || 0,
      receipt.last_read_message_id,
    );
    const summary = chats.find((chat) => chat.id === selectedChat.id);
    if (summary) summary.last_read_outbox_message_id = selectedChat.last_read_outbox_message_id;
  }
  messages.forEach((message) => {
    if (receipt.direction === "outbox" && message.is_outgoing && message.id <= receipt.last_read_message_id) message.is_read = true;
    if (receipt.direction === "inbox" && !message.is_outgoing && message.id <= receipt.last_read_message_id) message.is_read = true;
  });
  renderThread();
}

function composerHasContent() {
  return messageInput.value.replaceAll("\n", "").length > 0;
}

function updateComposer() {
  const keepLatestVisible = isThreadNearBottom();
  const hasContent = composerHasContent();
  actionControl.textContent = hasContent ? "S." : "O.";
  actionControl.classList.toggle("is-send", hasContent);
  actionControl.setAttribute("aria-label", hasContent ? "Send message" : "Hold to record a video message");
  messageInput.style.height = "auto";
  const maxHeight = 5 * 20;
  const nextHeight = Math.max(20, Math.min(messageInput.scrollHeight, maxHeight));
  messageInput.style.height = `${nextHeight}px`;
  messageInput.style.overflowY = messageInput.scrollHeight > maxHeight ? "auto" : "hidden";
  app.style.setProperty("--composer-height", `${nextHeight}px`);
  if (hasContent) sendTypingState();
  else void sendChatAction("cancel");
  if (keepLatestVisible) requestAnimationFrame(() => scrollThreadToBottom());
}

function syncVisualViewport() {
  const viewport = window.visualViewport;
  const height = viewport?.height || window.innerHeight;
  app.style.setProperty("--app-height", `${Math.round(height)}px`);
}

function sendTypingState() {
  const now = Date.now();
  if (now - lastTypingSentAt > 4000) {
    lastTypingSentAt = now;
    void sendChatAction("typing");
  }
  window.clearTimeout(typingCancelTimer);
  typingCancelTimer = window.setTimeout(() => void sendChatAction("cancel"), 5000);
}

async function sendChatAction(action, progress = 0, chatId = selectedChat?.id) {
  if (!chatId) return;
  try {
    await apiRequest(`/api/chats/${chatId}/actions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, progress }),
    });
  } catch {
    // Activity is best-effort; message delivery remains usable.
  }
}

function createOptimisticMessage(chatId, { kind = "text", text = "", media = null } = {}) {
  const id = optimisticMessageId--;
  return {
    id,
    chat_id: chatId,
    sender_id: account?.id || null,
    sender_type: "user",
    is_outgoing: true,
    sent_at: new Date().toISOString(),
    kind,
    text,
    entities: [],
    media,
    is_read: false,
    sending_state: "pending",
    client_pending: true,
  };
}

async function sendTextMessage() {
  if (!selectedChat || !composerHasContent()) return;
  const targetChat = selectedChat;
  const text = messageInput.value;
  messageInput.value = "";
  updateComposer();
  const optimistic = createOptimisticMessage(targetChat.id, { text });
  messages.push(optimistic);
  messages.sort(compareMessages);
  renderThread({ animateNew: true });
  try {
    const sent = await apiRequest(`/api/chats/${targetChat.id}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, entities: [] }),
    });
    if (selectedChat?.id === targetChat.id) {
      replaceOptimisticMessage(optimistic.id, sent);
      renderThread();
    } else {
      void loadChats();
    }
  } catch (error) {
    if (error.status !== 401) {
      if (selectedChat?.id === targetChat.id) {
        messages = messages.filter((message) => message.id !== optimistic.id);
        messageInput.value = text;
        updateComposer();
        renderThread();
      }
      setContactStatus("CNT");
    }
  } finally {
    void sendChatAction("cancel");
  }
}

async function selectedMediaMetadata(file) {
  const url = URL.createObjectURL(file);
  try {
    if (file.type.startsWith("image/")) {
      const image = new Image();
      image.src = url;
      await image.decode();
      return { duration: 0, width: image.naturalWidth || 0, height: image.naturalHeight || 0 };
    }
    if (!file.type.startsWith("video/")) return { duration: 0, width: 0, height: 0 };
    const video = document.createElement("video");
    video.preload = "metadata";
    video.src = url;
    await new Promise((resolve, reject) => {
      video.onloadedmetadata = resolve;
      video.onerror = reject;
    });
    return { duration: Math.ceil(video.duration || 0), width: video.videoWidth || 0, height: video.videoHeight || 0 };
  } finally {
    URL.revokeObjectURL(url);
  }
}

async function sendSelectedMedia(file) {
  if (!selectedChat) return;
  const targetChat = selectedChat;
  const kind = file.type.startsWith("video/") ? "video" : "photo";
  const action = kind === "video" ? "uploading_video" : "uploading_photo";
  let optimistic = null;
  try {
    const metadata = await selectedMediaMetadata(file);
    const localUrl = URL.createObjectURL(file);
    objectUrls.add(localUrl);
    optimistic = createOptimisticMessage(targetChat.id, {
      kind,
      media: {
        kind,
        file_id: optimisticMessageId,
        download_url: "",
        local_url: localUrl,
        mime_type: file.type,
        width: metadata.width,
        height: metadata.height,
        duration: metadata.duration,
        is_opened: false,
      },
    });
    if (selectedChat?.id === targetChat.id) {
      messages.push(optimistic);
      messages.sort(compareMessages);
      renderThread({ animateNew: true });
    }
    await sendChatAction(action, 0);
    const form = new FormData();
    form.set("file", file, file.name);
    form.set("kind", kind);
    form.set("caption", "");
    form.set("duration", String(metadata.duration));
    form.set("width", String(metadata.width));
    form.set("height", String(metadata.height));
    const sent = await apiRequest(`/api/chats/${targetChat.id}/media`, { method: "POST", body: form });
    if (selectedChat?.id === targetChat.id && optimistic) {
      replaceOptimisticMessage(optimistic.id, sent);
      renderThread();
    } else {
      void loadChats();
    }
  } catch (error) {
    if (error.status !== 401) {
      if (optimistic && selectedChat?.id === targetChat.id) {
        optimistic.sending_state = "failed";
        optimistic.client_pending = false;
        renderThread();
      }
      setContactStatus("CNT");
    }
  } finally {
    void sendChatAction("cancel");
  }
}

async function markContentOpened(message) {
  if (!selectedChat || message.is_outgoing) return;
  try {
    await apiRequest(`/api/chats/${selectedChat.id}/messages/${message.id}/open`, { method: "POST" });
  } catch {
    // Playback should continue even if the read acknowledgement is delayed.
  }
}

async function toggleVideoNote(button, video, message) {
  if (!button.classList.contains("is-focused")) {
    collapseFocusedVideo(button);
    const startRect = button.getBoundingClientRect();
    const appRect = app.getBoundingClientRect();
    button.style.setProperty("--focused-left", `${appRect.left + 36}px`);
    button.style.setProperty("--focused-bottom", `${window.innerHeight - startRect.bottom}px`);
    button.style.setProperty("--focused-size", `${appRect.width - 72}px`);
    button.classList.add("is-focused", "is-playing");
    button.closest(".video-message")?.classList.add("is-video-focused");
    button.classList.remove("is-background-playing");
    button.dataset.playing = "sound";
    button.setAttribute("aria-label", "Pause video message");
    chatScreen.classList.add("has-focused-video", "is-video-playing");
    video.loop = false;
    video.muted = false;
    animateVideoFlip(button, startRect);
    await video.play().catch(() => {});
    void markContentOpened(message);
  } else if (!video.paused) {
    video.pause();
    button.classList.remove("is-playing");
    button.dataset.playing = "paused";
    button.setAttribute("aria-label", "Resume video message with sound");
    chatScreen.classList.remove("is-video-playing");
  } else {
    button.classList.add("is-playing");
    button.dataset.playing = "sound";
    button.setAttribute("aria-label", "Pause video message");
    chatScreen.classList.add("is-video-playing");
    await video.play().catch(() => {});
  }
}

function collapseFocusedVideo(except = null) {
  const focused = chatScreen.querySelector(".video-note.is-focused");
  if (!focused || focused === except) return;
  const startRect = focused.getBoundingClientRect();
  const video = focused.querySelector("video");
  focused.classList.remove("is-focused", "is-playing");
  focused.closest(".video-message")?.classList.remove("is-video-focused");
  focused.classList.add("is-background-playing");
  focused.dataset.playing = "background";
  focused.setAttribute("aria-label", "Play video message with sound");
  focused.style.removeProperty("--focused-left");
  focused.style.removeProperty("--focused-bottom");
  focused.style.removeProperty("--focused-size");
  chatScreen.classList.remove("has-focused-video", "is-video-playing");
  animateVideoFlip(focused, startRect);
  if (video) {
    video.muted = true;
    video.loop = true;
    void video.play().catch(() => {});
  }
}

function animateVideoFlip(element, startRect) {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const endRect = element.getBoundingClientRect();
  if (!startRect.width || !endRect.width) return;
  element.animate([
    {
      transform: `translate(${startRect.left - endRect.left}px, ${startRect.top - endRect.top}px) scale(${startRect.width / endRect.width})`,
    },
    { transform: "translate(0, 0) scale(1)" },
  ], {
    duration: 300,
    easing: "cubic-bezier(0.2, 0, 0, 1)",
  });
}

function beginVideoHold(event) {
  if (recording || composerHasContent()) return;
  event.preventDefault();
  window.clearTimeout(holdTimer);
  holdTimer = window.setTimeout(() => {
    ignoreNextActionClick = true;
    void startRecording();
  }, HOLD_DELAY_MS);
}

function endVideoHold() {
  window.clearTimeout(holdTimer);
  holdTimer = null;
}

function cameraConstraints({ audio }) {
  return {
    audio,
    video: {
      facingMode: { ideal: facingMode },
      width: { ideal: 640 },
      height: { ideal: 640 },
      aspectRatio: { ideal: 1 },
    },
  };
}

async function acquireCamera() {
  cameraStream?.getTracks().forEach((track) => track.stop());
  cameraStream = await navigator.mediaDevices.getUserMedia(cameraConstraints({ audio: true }));
  recordingPreview.srcObject = cameraStream;
  await recordingPreview.play();
}

function preferredRecorderMimeType() {
  const candidates = [
    "video/mp4;codecs=h264,aac",
    "video/webm;codecs=vp9,opus",
    "video/webm;codecs=vp8,opus",
    "video/webm",
  ];
  return candidates.find((type) => window.MediaRecorder?.isTypeSupported(type)) || "";
}

function drawRecordingFrame() {
  if (!recording || !recordingPreview.videoWidth) {
    recordingDrawFrame = requestAnimationFrame(drawRecordingFrame);
    return;
  }
  const context = recordingCanvas.getContext("2d", { alpha: false });
  const sourceWidth = recordingPreview.videoWidth;
  const sourceHeight = recordingPreview.videoHeight;
  const side = Math.min(sourceWidth, sourceHeight);
  const sourceX = (sourceWidth - side) / 2;
  const sourceY = (sourceHeight - side) / 2;
  context.drawImage(recordingPreview, sourceX, sourceY, side, side, 0, 0, recordingCanvas.width, recordingCanvas.height);
  recordingDrawFrame = requestAnimationFrame(drawRecordingFrame);
}

function startMediaRecorder() {
  if (!window.MediaRecorder) throw new Error("MediaRecorder is unavailable.");
  const canvasStream = recordingCanvas.captureStream?.(30);
  recorderStream = canvasStream || cameraStream;
  if (canvasStream) {
    const audioTrack = cameraStream.getAudioTracks()[0];
    if (audioTrack) recorderStream.addTrack(audioTrack);
    drawRecordingFrame();
  }
  recorderChunks = [];
  const mimeType = preferredRecorderMimeType();
  mediaRecorder = new MediaRecorder(recorderStream, mimeType ? { mimeType } : undefined);
  recorderResult = new Promise((resolve) => {
    mediaRecorder.addEventListener("dataavailable", (event) => {
      if (event.data.size) recorderChunks.push(event.data);
    });
    mediaRecorder.addEventListener("stop", () => {
      const type = mediaRecorder.mimeType || recorderChunks[0]?.type || "video/webm";
      resolve(new Blob(recorderChunks, { type }));
    }, { once: true });
  });
  mediaRecorder.start(1000);
}

async function startRecording() {
  if (recording || !selectedChat) return;
  recording = true;
  recordingStartedAt = performance.now();
  recordingLayer.hidden = false;
  composer.hidden = true;
  chatScreen.classList.add("is-recording");
  recordingPreview.classList.toggle("is-front", facingMode === "user");
  tickRecordingTimer();
  try {
    await acquireCamera();
    startMediaRecorder();
    void sendChatAction("recording_video_note");
  } catch {
    await stopRecording(false);
    setContactStatus("CNT");
  }
}

function tickRecordingTimer() {
  if (!recording) return;
  const elapsed = Math.min(performance.now() - recordingStartedAt, VIDEO_NOTE_LIMIT_MS);
  recordingTimer.value = formatDuration(elapsed);
  if (elapsed >= VIDEO_NOTE_LIMIT_MS) {
    void stopRecording(true);
    return;
  }
  recordingFrame = requestAnimationFrame(tickRecordingTimer);
}

function formatDuration(milliseconds) {
  const hundredths = Math.floor(milliseconds / 10);
  const seconds = Math.floor(hundredths / 100);
  return `${seconds}.${String(hundredths % 100).padStart(2, "0")}`;
}

async function stopRecording(send) {
  if (!recording) return;
  const duration = Math.min(performance.now() - recordingStartedAt, VIDEO_NOTE_LIMIT_MS);
  recording = false;
  cancelAnimationFrame(recordingFrame);
  cancelAnimationFrame(recordingDrawFrame);
  let blob = null;
  if (mediaRecorder && mediaRecorder.state !== "inactive") {
    mediaRecorder.stop();
    blob = await recorderResult;
  }
  recorderStream?.getTracks().forEach((track) => track.stop());
  cameraStream?.getTracks().forEach((track) => track.stop());
  recorderStream = null;
  cameraStream = null;
  mediaRecorder = null;
  recordingPreview.srcObject = null;
  recordingLayer.hidden = true;
  composer.hidden = false;
  chatScreen.classList.remove("is-recording");
  recordingTimer.value = "0.00";
  void sendChatAction("cancel");
  if (send && blob?.size) enqueueVideoNoteUpload(blob, duration);
}

function enqueueVideoNoteUpload(blob, durationMs) {
  if (!selectedChat) return;
  const targetChatId = selectedChat.id;
  const localUrl = URL.createObjectURL(blob);
  objectUrls.add(localUrl);
  const optimistic = createOptimisticMessage(targetChatId, {
    kind: "video_note",
    media: {
      kind: "video_note",
      file_id: optimisticMessageId,
      download_url: "",
      local_url: localUrl,
      mime_type: blob.type,
      width: 480,
      height: 480,
      duration: Math.max(1, Math.ceil(durationMs / 1000)),
      is_opened: false,
    },
  });
  messages.push(optimistic);
  messages.sort(compareMessages);
  renderThread({ animateNew: true });
  videoNoteUploadQueue = videoNoteUploadQueue
    .catch(() => {})
    .then(() => uploadVideoNote(blob, durationMs, targetChatId, optimistic));
}

async function uploadVideoNote(blob, durationMs, targetChatId, optimistic) {
  const extension = blob.type.includes("mp4") ? "mp4" : "webm";
  const form = new FormData();
  form.set("file", new File([blob], `video-note.${extension}`, { type: blob.type }), `video-note.${extension}`);
  form.set("kind", "video_note");
  form.set("caption", "");
  form.set("duration", String(Math.max(1, Math.ceil(durationMs / 1000))));
  form.set("width", "480");
  form.set("height", "480");
  try {
    await sendChatAction("uploading_video_note", 0, targetChatId);
    const sent = await apiRequest(`/api/chats/${targetChatId}/media`, { method: "POST", body: form });
    if (selectedChat?.id === targetChatId) {
      replaceOptimisticMessage(optimistic.id, sent);
      renderThread();
    } else {
      void loadChats();
    }
  } catch (error) {
    if (error.status !== 401) {
      if (selectedChat?.id === targetChatId) {
        optimistic.sending_state = "failed";
        optimistic.client_pending = false;
        renderThread();
      }
      setContactStatus("CNT");
    }
  } finally {
    void sendChatAction("cancel", 0, targetChatId);
  }
}

async function switchCamera() {
  if (!recording || !cameraStream) return;
  facingMode = facingMode === "user" ? "environment" : "user";
  recordingPreview.classList.toggle("is-front", facingMode === "user");
  try {
    const replacement = await navigator.mediaDevices.getUserMedia(cameraConstraints({ audio: false }));
    const audioTracks = cameraStream.getAudioTracks();
    cameraStream.getVideoTracks().forEach((track) => track.stop());
    cameraStream = new MediaStream([...replacement.getVideoTracks(), ...audioTracks]);
    recordingPreview.srcObject = cameraStream;
    await recordingPreview.play();
  } catch {
    facingMode = facingMode === "user" ? "environment" : "user";
    recordingPreview.classList.toggle("is-front", facingMode === "user");
  }
}

async function openFromUrl() {
  const params = new URLSearchParams(window.location.search);
  app.classList.toggle("show-grid", params.get("grid") === "1");
  const chatId = Number(params.get("chat"));
  const chat = chats.find((item) => item.id === chatId);
  if (params.get("view") === "chat" && chat) await openChat(chat);
  else await showMain({ refresh: false });
}

async function boot() {
  setConnectionState("CNT");
  try {
    await consumeAccessLink();
    if (IS_PHOTODE_HOST && !accessToken) {
      showUnlock();
      return;
    }
    account = await apiRequest("/api/telegram/me");
    accountName.textContent = account.display_name;
    void setTelegramBowlColors(accountBowl, account, account.id);
    await loadChats();
    showApp();
    await openFromUrl();
  } catch (error) {
    if (error.status === 401 || (IS_PHOTODE_HOST && !accessToken)) showUnlock(error.message);
    else {
      showApp();
      setConnectionState("CNT");
    }
  }
}

unlockForm.addEventListener("submit", (event) => {
  event.preventDefault();
  accessError.value = "Connecting";
  void exchangeAccessKey(accessKeyInput.value).then(() => {
    accessKeyInput.value = "";
    return boot();
  }).catch((error) => {
    accessError.value = error.message;
  });
});

chatBack.addEventListener("click", () => void showMain({ push: true }));
messageInput.addEventListener("input", updateComposer);
composer.addEventListener("submit", (event) => event.preventDefault());
actionControl.addEventListener("pointerdown", beginVideoHold);
actionControl.addEventListener("pointerup", endVideoHold);
actionControl.addEventListener("pointercancel", endVideoHold);
actionControl.addEventListener("pointerleave", endVideoHold);
actionControl.addEventListener("contextmenu", (event) => event.preventDefault());
actionControl.addEventListener("click", () => {
  if (ignoreNextActionClick) {
    ignoreNextActionClick = false;
    return;
  }
  if (composerHasContent()) void sendTextMessage();
});
mediaControl.addEventListener("click", () => mediaInput.click());
mediaInput.addEventListener("change", () => {
  const [file] = mediaInput.files || [];
  if (file) void sendSelectedMedia(file);
  mediaInput.value = "";
});
recordingCancel.addEventListener("click", () => void stopRecording(false));
recordingSend.addEventListener("click", () => void stopRecording(true));
recordingSwitch.addEventListener("click", () => void switchCamera());
chatScreen.addEventListener("click", (event) => {
  if (chatScreen.querySelector(".video-note.is-focused") && !event.target.closest(".video-note")) collapseFocusedVideo();
});
messageScroller.addEventListener("touchstart", () => { scrollIntentVersion += 1; }, { passive: true });
messageScroller.addEventListener("wheel", () => { scrollIntentVersion += 1; }, { passive: true });
window.addEventListener("popstate", () => void openFromUrl());
window.addEventListener("resize", syncVisualViewport);
window.visualViewport?.addEventListener("resize", syncVisualViewport);
window.visualViewport?.addEventListener("scroll", syncVisualViewport);
window.addEventListener("beforeunload", () => {
  closeEventSocket();
  cameraStream?.getTracks().forEach((track) => track.stop());
  recorderStream?.getTracks().forEach((track) => track.stop());
  objectUrls.forEach((url) => URL.revokeObjectURL(url));
});

updateComposer();
void boot();
