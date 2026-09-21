import { apiRequest } from './modules/api.js';
import { createState, sortedStocks, indexStocks } from './modules/state.js';
import { createCharts } from './modules/charts.js';
import { createDashboard } from './modules/dashboard.js';
import { createConfig } from './modules/config.js';
import { createJournal } from './modules/journal.js';

// Keep the Trading module in the repository for a future re-enable, but do
// not expose or load it while broker trading is not in use.
const ENABLE_TRADING = false;

const state = createState();
let pageLoadId = 0;
let stockRevision = 0;
const stockMutationTokens = new Map();
function replaceStocks(next) { state.stocks = sortedStocks(next); state.stockMap = indexStocks(state.stocks); }
function upsertStock(stock) { replaceStocks([...state.stocks.filter(item => item.id !== stock.id), stock]); }
function removeStockFromState(id) { replaceStocks(state.stocks.filter(stock => stock.id !== id)); }
function beginStockMutation(id) { const token=(stockMutationTokens.get(id)||0)+1; stockMutationTokens.set(id, token); stockRevision += 1; return token; }
function isCurrentStockMutation(id, token) { return stockMutationTokens.get(id) === token; }
function showA(message, type='i', containerId='ac') { const container=document.getElementById(containerId); if(!container)return; const alert=document.createElement('div'); alert.className=`alert-box ${type}`; alert.textContent=message; container.appendChild(alert); setTimeout(()=>alert.remove(),5000); }
function pageFromHash(hash=window.location.hash) { const page=hash.replace('#/','')||'dashboard'; return ['dashboard','config','journal', ...(ENABLE_TRADING ? ['trading'] : [])].includes(page)?page:'dashboard'; }
function showPage(page) { document.querySelectorAll('nav a').forEach(link=>link.classList.toggle('active',link.dataset.page===page)); ['dashboard','config','journal'].forEach(name=>document.getElementById(`page-${name}`).classList.toggle('hidden',name!==page)); }
function dt(id) { document.getElementById(id)?.classList.toggle('open'); }
async function af(path, options={}, onError) { return apiRequest(path, options, (message, details)=>onError ? onError(message, details) : showA(message,'error')); }
const charts = createCharts({ request:af, historyCache:state.historyCache });
function renderCurrentPage() { const page=pageFromHash(); showPage(page); if(page==='dashboard')dashboard.renderDashboard(); else if(page==='journal')journal.renderJournal(); else config.renderConfig(); }
const context = { getStocks:()=>state.stocks, getStockMap:()=>state.stockMap, getTrades:()=>state.trades, getTradePagination:()=>state.tradePagination, getCapitalEvents:()=>state.capitalEvents, getAnalytics:()=>state.analytics, request:af, replaceStocks, upsertStock, removeStockFromState, beginStockMutation, isCurrentStockMutation, getStockRevision:()=>stockRevision, incrementStockRevision:()=>++stockRevision, showA, renderCurrentPage, pageFromHash, nav, charts, actions: {}, setJournalPage:page=>{state.journalPage=page;}, reloadJournal:(page=state.journalPage)=>loadPage('journal', page) };
const dashboard = createDashboard(context);
const config = createConfig(context);
const journal = createJournal(context);
Object.assign(context.actions, { edit: config.es, toggle: config.ts, remove: config.rs });
function nav(hash) { const page=pageFromHash(hash); const target=page==='dashboard'?'':`#/${page}`; if(window.location.hash!==target){window.location.hash=target;return;} loadPage(page); }
async function loadPage(page, journalPage=state.journalPage) { const loadId=++pageLoadId; const startRevision=stockRevision; showPage(page); document.getElementById(`page-${page}`).innerHTML='<div class="loading">Loading...</div>'; if(page==='dashboard'){const data=await af('/dashboard'); if(loadId!==pageLoadId||pageFromHash()!==page)return; if(data&&stockRevision===startRevision)replaceStocks(data.stocks); dashboard.renderDashboard(); return;} if(page==='journal'){const requestedPage=Math.max(1, Number(journalPage) || 1); const [freshStocks,freshTrades,freshEvents,freshAnalytics]=await Promise.all([af('/stocks?includeArchived=1'),af(`/trades?page=${requestedPage}&page_size=20`),af('/capital-events'),af('/analytics/dashboard')]); if(loadId!==pageLoadId||pageFromHash()!==page)return; if(freshStocks&&stockRevision===startRevision)replaceStocks(freshStocks); if(freshTrades){state.trades=freshTrades.items; state.tradePagination={page:freshTrades.page,page_size:freshTrades.page_size,total:freshTrades.total,total_pages:freshTrades.total_pages}; state.journalPage=freshTrades.page;} if(freshEvents)state.capitalEvents=freshEvents; if(freshAnalytics)state.analytics=freshAnalytics; journal.renderJournal(); return;} const freshStocks=await af('/stocks'); if(loadId!==pageLoadId||pageFromHash()!==page)return; if(freshStocks&&stockRevision===startRevision)replaceStocks(freshStocks); config.renderConfig(); }
Object.assign(window, { ...dashboard, ...config, ...journal, nav, dt, showA });
window.addEventListener('hashchange',()=>loadPage(pageFromHash()));
document.addEventListener('click',event=>{ if(!event.target.closest('.dropdown'))document.querySelectorAll('.dropdown.open').forEach(dropdown=>dropdown.classList.remove('open')); });
loadPage(pageFromHash());
