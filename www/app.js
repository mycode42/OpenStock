import { apiRequest } from './modules/api.js';
import { createState, sortedStocks, indexStocks } from './modules/state.js';
import { createCharts } from './modules/charts.js?demo-mode=1&alpaca-usd-only=1';
import { createDashboard } from './modules/dashboard.js?title-cleanup=1&demo-mode=1&alpaca-usd-only=1';
import { createJournal } from './modules/journal.js?trade-pagination=2&title-cleanup=1&alpaca-usd-only=1';
import { createAlgoTrader } from './modules/algo-trader.js?v=3.1.0&research-lab=3&options-api-guard=1&strategy-scorecard=2&appearance=1&optimizer-table=1&chart-range=1&coordinator-regime=1&research-page=1&demo-mode=1&split-modules=1';
import { createSettings } from './modules/settings.js';
import { createConfig } from './modules/config.js';
import { createAlgoUniverse } from './modules/algo-universe.js?cards=1&demo-mode=1';

const state = createState();
let pageLoadId = 0;
let renderedPage = null;
let lastConfigFocusHash = null;
let stockRevision = 0;
const stockMutationTokens = new Map();
function replaceStocks(next) { state.stocks = sortedStocks(next); state.stockMap = indexStocks(state.stocks); }
function upsertStock(stock) { replaceStocks([...state.stocks.filter(item => item.id !== stock.id), stock]); }
function removeStockFromState(id) { replaceStocks(state.stocks.filter(stock => stock.id !== id)); }
function beginStockMutation(id) { const token=(stockMutationTokens.get(id)||0)+1; stockMutationTokens.set(id, token); stockRevision += 1; return token; }
function isCurrentStockMutation(id, token) { return stockMutationTokens.get(id) === token; }
function showA(message, type='i', containerId='ac') { const container=document.getElementById(containerId); if(!container)return; const alert=document.createElement('div'); alert.className=`alert-box ${type}`; alert.textContent=message; container.appendChild(alert); setTimeout(()=>alert.remove(),5000); }
function pageFromHash(hash=window.location.hash) { const page=hash.replace('#/','').split('?')[0]||'dashboard'; return ['dashboard','algo-universe','journal','config','algo-trader','algo-research','settings'].includes(page)?page:'dashboard'; }
function showPage(page) { document.querySelectorAll('nav a').forEach(link=>link.classList.toggle('active',link.dataset.page===page)); ['dashboard','algo-universe','journal','config','algo-trader','algo-research','settings'].forEach(name=>document.getElementById(`page-${name}`).classList.toggle('hidden',name!==page)); }
function dt(id) { document.getElementById(id)?.classList.toggle('open'); }
async function af(path, options={}, onError) { return apiRequest(path, options, (message, details)=>onError ? onError(message, details) : showA(message,'error')); }
apiRequest('/demo-status').then(status => {
    if (!status?.demo_mode) return;
    const banner = document.getElementById('demo-mode-banner');
    if (!banner) return;
    banner.hidden = false;
});
const charts = createCharts({ request:af, historyCache:state.historyCache });
function renderCurrentPage() { const page=pageFromHash(); showPage(page); if(page==='dashboard')dashboard.renderDashboard(); else if(page==='journal')journal.renderJournal(); else if(page==='algo-trader')algoTrader.render(); else if(page==='algo-research')algoTrader.renderResearch(); else if(page==='algo-universe')algoUniverse.render(); else if(page==='settings')settings.render(); }
const context = { getStocks:()=>state.stocks, getStockMap:()=>state.stockMap, getTrades:()=>state.trades, getTradePagination:()=>state.tradePagination, getCapitalEvents:()=>state.capitalEvents, getAnalytics:()=>state.analytics, request:af, replaceStocks, upsertStock, removeStockFromState, beginStockMutation, isCurrentStockMutation, getStockRevision:()=>stockRevision, incrementStockRevision:()=>++stockRevision, showA, renderCurrentPage, reloadCurrentPage:()=>loadPage(pageFromHash()), pageFromHash, nav, charts, setJournalPage:page=>{state.journalPage=page;}, reloadJournal:(page=state.journalPage)=>loadPage('journal', page) };
const dashboard = createDashboard(context);
const journal = createJournal(context);
const algoTrader = createAlgoTrader({ request:af, charts });
const algoUniverse = createAlgoUniverse({ request:af, charts });
const settings = createSettings();
const config = createConfig({ request:af, reload:()=>loadPage(pageFromHash()), notify:showA });
settings.initialize();
function nav(hash) { const page=pageFromHash(hash); const target=page==='dashboard'?'':`#/${page}`; if(window.location.hash!==target){window.location.hash=target;return;} loadPage(page); }
async function loadPage(page, journalPage=state.journalPage) {
    const loadId=++pageLoadId;
    const startRevision=stockRevision;
    if(page!=='dashboard')dashboard.stopLivePolling();
    if(page!=='algo-trader'&&page!=='algo-research')algoTrader.stopPolling();
    if(page!=='algo-universe')algoUniverse.stopPolling();
    if(page!=='config')config.stopLivePolling();
    showPage(page);
    // A refresh keeps the current content until new data arrives, so saves and rearms do not blank the screen.
    if(renderedPage!==page)document.getElementById(`page-${page}`).innerHTML='<div class="loading">Loading...</div>';
    renderedPage=page;
    if(page==='settings'){
        settings.render();
        return;
    }
    if(page==='dashboard'){
        const data=await af('/dashboard');
        if(loadId!==pageLoadId||pageFromHash()!==page)return;
        if(!data){document.getElementById('page-dashboard').innerHTML='<header><h1>OpenStock</h1></header><div class="alert-box error">Dashboard market data is unavailable.</div>';return;}
        if(data&&stockRevision===startRevision)replaceStocks(data.stocks);
        dashboard.renderDashboard();
        return;
    }
    if(page==='config'){
        const [data, orders]=await Promise.all([af('/dashboard'), af('/alpaca/orders/config')]);
        if(loadId!==pageLoadId||pageFromHash()!==page)return;
        if(!data){document.getElementById('page-config').innerHTML='<header><h1>Configure</h1></header><div class="alert-box error">Alpaca alert levels are unavailable.</div>';return;}
        const query=new URLSearchParams(window.location.hash.split('?')[1]||'');
        if(query.get('stock')&&window.location.hash!==lastConfigFocusHash){ lastConfigFocusHash=window.location.hash; config.focus(query.get('stock'), query.get('level')); }
        config.render(data.stocks, orders?.orders);
        return;
    }
    if(page==='journal'){
        const data=await af('/alpaca/journal');
        if(loadId!==pageLoadId||pageFromHash()!==page)return;
        if(data)journal.renderAlpacaJournal(data);
        else document.getElementById('page-journal').innerHTML='<header><h1>Trading Journal</h1><p class="subtitle">Alpaca · USD</p></header><div class="alert-box error">Alpaca account activity is unavailable.</div>';
        return;
    }
    if(page==='algo-trader'){
        await algoTrader.load();
        if(loadId!==pageLoadId||pageFromHash()!==page)return;
        algoTrader.render();
        algoTrader.startPolling();
        return;
    }
    if(page==='algo-research'){
        await algoTrader.loadResearch();
        if(loadId!==pageLoadId||pageFromHash()!==page)return;
        algoTrader.renderResearch();
        algoTrader.startResearchPolling();
        return;
    }
    if(page==='algo-universe'){
        algoUniverse.startPolling();
        return;
    }
}
Object.assign(window, { ...dashboard, ...journal, nav, dt, showA });
function enableStandalonePullToRefresh() {
  const indicator = document.getElementById('pull-refresh-indicator');
  const standalone = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  if (!standalone || !indicator) return;
  document.documentElement.classList.add('pwa-standalone');

  const label = indicator.querySelector('.pull-refresh-label');
  const icon = indicator.querySelector('.pull-refresh-icon');
  const threshold = 88;
  let gesture = null;

  function hideIndicator() {
    indicator.classList.remove('is-pulling', 'is-ready', 'is-refreshing');
    indicator.style.removeProperty('--pull-offset');
    indicator.setAttribute('aria-hidden', 'true');
    label.textContent = 'Pull to refresh';
    icon.textContent = '↓';
  }

  document.addEventListener('touchstart', event => {
    gesture = null;
    hideIndicator();
    const scrollTop = document.scrollingElement?.scrollTop ?? window.scrollY;
    if (event.touches.length !== 1 || scrollTop > 0) return;
    const target = event.target instanceof Element ? event.target : null;
    if (target?.closest('input, textarea, select, [contenteditable="true"]')) return;
    const touch = event.touches[0];
    gesture = { startX: touch.clientX, startY: touch.clientY, distance: 0 };
  }, { passive: true });

  document.addEventListener('touchmove', event => {
    if (!gesture) return;
    if (event.touches.length !== 1) { gesture = null; hideIndicator(); return; }
    const touch = event.touches[0];
    const deltaX = touch.clientX - gesture.startX;
    const deltaY = touch.clientY - gesture.startY;
    const scrollTop = document.scrollingElement?.scrollTop ?? window.scrollY;
    if (deltaY <= 0 || Math.abs(deltaX) > Math.max(12, deltaY * .65) || scrollTop > 0) {
      gesture = null;
      hideIndicator();
      return;
    }
    if (deltaY < 10) return;

    gesture.distance = deltaY;
    indicator.style.setProperty('--pull-offset', `${Math.min(deltaY - 8, 72)}px`);
    indicator.classList.add('is-pulling');
    indicator.setAttribute('aria-hidden', 'false');
    if (deltaY >= threshold) {
      indicator.classList.add('is-ready');
      label.textContent = 'Release to refresh';
      icon.textContent = '↑';
    } else {
      indicator.classList.remove('is-ready');
      label.textContent = 'Pull to refresh';
      icon.textContent = '↓';
    }
  }, { passive: true });

  document.addEventListener('touchend', event => {
    if (event.touches.length > 0) { gesture = null; hideIndicator(); return; }
    if (!gesture) return;
    const shouldRefresh = gesture.distance >= threshold;
    gesture = null;
    if (!shouldRefresh) { hideIndicator(); return; }
    indicator.classList.remove('is-pulling', 'is-ready');
    indicator.classList.add('is-refreshing');
    label.textContent = 'Refreshing…';
    icon.textContent = '↻';
    indicator.setAttribute('aria-hidden', 'false');
    window.location.reload();
  }, { passive: true });

  document.addEventListener('touchcancel', () => { gesture = null; hideIndicator(); }, { passive: true });
}
enableStandalonePullToRefresh();
window.addEventListener('hashchange',()=>loadPage(pageFromHash()));
document.addEventListener('click',event=>{ if(!event.target.closest('.dropdown'))document.querySelectorAll('.dropdown.open').forEach(dropdown=>dropdown.classList.remove('open')); });
loadPage(pageFromHash());
