require('dotenv').config();
const express = require('express');
const cors = require('cors');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const cron = require('node-cron');
const nodemailer = require('nodemailer');

const app = express();
const PORT = process.env.PORT || 5000;
const MASK = '********';

app.use(cors());
app.use(express.json());

const DB_PATH = path.join(__dirname, 'data.json');

let scannerState = { running: false, logs: [], error: null, startedAt: null };

function readDb() {
  try {
    if (!fs.existsSync(DB_PATH)) return { config: {}, recommendations: [] };
    return JSON.parse(fs.readFileSync(DB_PATH, 'utf8'));
  } catch (e) {
    console.error('Error reading database:', e);
    return { config: {}, recommendations: [] };
  }
}

function writeDb(data) {
  try {
    fs.writeFileSync(DB_PATH, JSON.stringify(data, null, 2), 'utf8');
    return true;
  } catch (e) {
    console.error('Error writing database:', e);
    return false;
  }
}

function addLog(message) {
  const line = `[${new Date().toLocaleTimeString()}] ${message}`;
  console.log(line);
  scannerState.logs.push(line);
  if (scannerState.logs.length > 500) scannerState.logs.shift();
}

// Secrets live in .env, never in data.json and never in API responses.
function smtpPass(config) {
  return process.env.SMTP_PASS || (config.email && config.email.smtpPass) || '';
}

function sanitizeConfig(config) {
  const c = JSON.parse(JSON.stringify(config || {}));
  if (c.email && c.email.smtpPass) c.email.smtpPass = MASK;
  if (c.geminiApiKey) c.geminiApiKey = MASK;
  return c;
}

// ---------------------------------------------------------------- email
async function sendRecommendationEmail(recs, config) {
  if (!config.email || !config.email.enabled) {
    addLog('Email notifications are disabled.');
    return;
  }
  const { smtpHost, smtpPort, smtpUser, toEmail } = config.email;
  const pass = smtpPass(config);
  if (!smtpHost || !smtpUser || !pass || !toEmail) {
    addLog('Email configuration incomplete (set SMTP_PASS in .env). Skipping.');
    return;
  }

  addLog(`Sending email to ${toEmail}...`);
  const transporter = nodemailer.createTransport({
    host: smtpHost,
    port: parseInt(smtpPort),
    secure: parseInt(smtpPort) === 465,
    auth: { user: smtpUser, pass },
    // TLS certificate verification stays ON - do not disable it.
  });

  const dateStr = new Date().toLocaleDateString('en-IN', { dateStyle: 'medium' });
  const aGrade = recs.filter(r => r.grade === 'A');
  const watch = recs.filter(r => r.grade === 'B');

  let html = `
    <div style="font-family:Arial,sans-serif;max-width:620px;margin:auto;padding:20px;">
      <h2 style="margin:0 0 4px;">SwingSense AI - ${dateStr}</h2>
      <p style="color:#555;margin:0 0 16px;">Swing shortlist, holding period 5-10 trading days.</p>`;

  if (aGrade.length === 0) {
    html += `<div style="padding:14px;background:#fff3cd;border-left:5px solid #ffc107;border-radius:4px;">
      <b>No A-grade swing opportunities identified today.</b></div>`;
  }
  const block = (rec, idx, tag, tagColor) => `
    <div style="margin:14px 0;padding:14px;border:1px solid #ddd;border-radius:8px;">
      <div style="display:flex;justify-content:space-between;border-bottom:1px solid #eee;padding-bottom:6px;">
        <b style="font-size:16px;">${idx}. ${rec.symbol} <span style="font-weight:400;color:#777;">${rec.companyName}</span></b>
        <span style="background:${tagColor};color:#fff;padding:2px 10px;border-radius:12px;font-size:12px;">${tag} ${(rec.breakoutScore ?? rec.confidenceScore)}/100</span>
      </div>
      <table style="width:100%;font-size:14px;margin-top:8px;">
        <tr><td style="color:#666;">Sector</td><td><b>${rec.sector}</b></td></tr>
        <tr><td style="color:#666;">Price</td><td><b>Rs ${rec.closePrice.toFixed(2)}</b></td></tr>
        <tr><td style="color:#28a745;">Buy zone</td><td><b>Rs ${rec.buyZone}</b></td></tr>
        <tr><td style="color:#dc3545;">Stop loss</td><td><b>Rs ${rec.stopLoss.toFixed(2)}</b></td></tr>
        <tr><td style="color:#0d6efd;">Target 1</td><td><b>Rs ${(rec.target1 ?? rec.target).toFixed(2)}</b></td></tr>
        <tr><td style="color:#0d6efd;">Target 2</td><td><b>${rec.target2 != null ? 'Rs ' + rec.target2.toFixed(2) : '-'}</b>${rec.riskReward != null ? ' &nbsp; (R:R ' + rec.riskReward + ')' : ''}</td></tr>
        <tr><td style="color:#666;">Risk</td><td><b>${rec.riskRating}</b> - hold ${rec.holdingPeriod}</td></tr>
      </table>
      <div style="font-size:13px;color:#555;background:#f9f9f9;padding:10px;border-radius:4px;margin-top:8px;">
        ${rec.aiExplanation.replace(/\n/g, '<br/>')}
      </div>
    </div>`;

  aGrade.forEach((r, i) => { html += block(r, i + 1, 'A-grade', '#0d6efd'); });
  if (watch.length) {
    html += `<h3 style="margin:20px 0 4px;">Watchlist (missed one or more filters)</h3>`;
    watch.forEach((r, i) => { html += block(r, i + 1, 'Watch', '#b58900'); });
  }
  html += `<p style="font-size:12px;color:#999;text-align:center;margin-top:24px;border-top:1px solid #eee;padding-top:10px;">
      SwingSense AI is an informational screening assistant, not investment advice. Trade at your own risk.
    </p></div>`;

  try {
    await transporter.sendMail({
      from: `"SwingSense AI" <${smtpUser}>`,
      to: toEmail,
      subject: `SwingSense AI: ${aGrade.length} A-grade signals for ${dateStr}`,
      html,
    });
    addLog('Email sent successfully.');
  } catch (e) {
    addLog(`Email delivery failed: ${e.message}`);
  }
}

// ---------------------------------------------------------------- scanner
function pythonCommand() {
  // Windows installs often expose "py" instead of "python".
  if (process.env.PYTHON_CMD) return process.env.PYTHON_CMD;
  return process.platform === 'win32' ? 'py' : 'python3';
}

function executeScanner() {
  if (scannerState.running) {
    addLog('Scanner is already running. Action rejected.');
    return;
  }
  scannerState.running = true;
  scannerState.logs = [];
  scannerState.error = null;
  scannerState.startedAt = new Date();

  const scriptPath = path.join(__dirname, 'scanner', 'scanner.py');
  const db = readDb();
  const env = {
    ...process.env,
    PYTHONWARNINGS: 'ignore',
    PYTHONIOENCODING: 'utf-8',
    GEMINI_API_KEY: process.env.GEMINI_API_KEY || db.config.geminiApiKey || '',
  };

  const tryCmds = [pythonCommand(), process.platform === 'win32' ? 'python' : 'py'];

  const run = (cmdIdx) => {
    const cmd = tryCmds[cmdIdx];
    addLog(`Starting scanner engine (${cmd})...`);
    const py = spawn(cmd, [scriptPath], { env });

    py.stdout.on('data', d => d.toString().split('\n')
      .forEach(l => l.trim() && addLog(l.trim())));
    py.stderr.on('data', d => d.toString().split('\n')
      .forEach(l => l.trim() && addLog(`[ERROR] ${l.trim()}`)));

    py.on('error', (err) => {
      if (err.code === 'ENOENT' && cmdIdx + 1 < tryCmds.length) {
        addLog(`${cmd} not found, trying ${tryCmds[cmdIdx + 1]}...`);
        run(cmdIdx + 1);
        return;
      }
      scannerState.running = false;
      scannerState.error = err.message;
      addLog(`[FATAL] Failed to start Python: ${err.message}`);
    });

    py.on('close', (code) => {
      if (code === null) return; // handled by error path
      scannerState.running = false;
      if (code === 0) {
        addLog('Scanner completed successfully.');
        const fresh = readDb();
        const today = new Date().toISOString().split('T')[0];
        const todays = fresh.recommendations.filter(r => r.date === today);
        addLog(`${todays.length} recommendation(s) for today.`);
        sendRecommendationEmail(todays, fresh.config);
      } else {
        scannerState.error = `Scanner failed with exit code ${code}`;
        addLog(`[FATAL] Scanner exited with code ${code}.`);
      }
    });
  };
  run(0);
}

// ---------------------------------------------------------------- API
app.get('/api/config', (req, res) => {
  res.json(sanitizeConfig(readDb().config));
});

app.post('/api/config', (req, res) => {
  const db = readDb();
  const incoming = req.body || {};
  // Preserve stored secrets when the client sends back the mask.
  if (incoming.email && incoming.email.smtpPass === MASK) {
    incoming.email.smtpPass = (db.config.email && db.config.email.smtpPass) || '';
  }
  if (incoming.geminiApiKey === MASK) {
    incoming.geminiApiKey = db.config.geminiApiKey || '';
  }
  db.config = incoming;
  if (writeDb(db)) res.json({ success: true, message: 'Configuration updated.' });
  else res.status(500).json({ success: false, message: 'Failed to update configuration.' });
});

app.get('/api/recommendations', (req, res) => {
  res.json(readDb().recommendations || []);
});

app.get('/api/status', (req, res) => {
  const db = readDb();
  res.json({ lastScan: db.lastScan || null, scanner: scannerState });
});

app.post('/api/recommendations/close', (req, res) => {
  const { id, exitPrice, status } = req.body;
  if (!id || !exitPrice || !status) {
    return res.status(400).json({ success: false, message: 'Missing required fields.' });
  }
  const db = readDb();
  const rec = db.recommendations.find(r => r.id === id);
  if (!rec) return res.status(404).json({ success: false, message: 'Recommendation not found.' });

  rec.status = status;
  rec.exitPrice = parseFloat(exitPrice);
  rec.exitDate = new Date().toISOString().split('T')[0];
  rec.pnlPct = Number((((rec.exitPrice - rec.closePrice) / rec.closePrice) * 100).toFixed(2));
  rec.closedBy = 'manual';

  if (writeDb(db)) res.json({ success: true, message: 'Position closed.', data: rec });
  else res.status(500).json({ success: false, message: 'Failed to write update.' });
});

app.post('/api/recommendations/run', (req, res) => {
  if (scannerState.running) {
    return res.status(400).json({ success: false, message: 'Scanner is already running.' });
  }
  executeScanner();
  res.json({ success: true, message: 'Scanner started in the background.' });
});

app.get('/api/scanner/status', (req, res) => res.json(scannerState));

app.post('/api/email/test', async (req, res) => {
  const db = readDb();
  if (!db.config.email || !db.config.email.smtpHost || !db.config.email.smtpUser) {
    return res.status(400).json({ success: false, message: 'SMTP details not configured in settings.' });
  }
  addLog('Sending test email...');
  const testRecs = [{
    symbol: 'TESTSTK', companyName: 'SwingSense Test Corp', sector: 'Information Technology',
    closePrice: 100.0, buyZone: '99.0 - 101.0', target: 108.0, stopLoss: 95.0,
    confidenceScore: 92, riskRating: 'Low', grade: 'A', holdingPeriod: '5-10 days',
    aiExplanation: 'Dummy recommendation to verify SMTP settings. If you can read this, email alerts work.',
  }];
  try {
    await sendRecommendationEmail(testRecs, db.config);
    res.json({ success: true, message: 'Test email attempted - check the logs and your inbox.' });
  } catch (e) {
    res.status(500).json({ success: false, message: `Failed to send email: ${e.message}` });
  }
});

// ---------------------------------------------------------------- static
app.use(express.static(path.join(__dirname, 'dist')));
app.get('*', (req, res) => {
  const index = path.join(__dirname, 'dist', 'index.html');
  if (fs.existsSync(index)) res.sendFile(index);
  else res.send('SwingSense backend is running, but dist/index.html is missing.');
});

// NSE publishes delivery data ~6:30 PM IST, so scan at 18:40 Mon-Fri.
cron.schedule('40 18 * * 1-5', () => {
  console.log('Cron: auto scanning market...');
  executeScanner();
});

app.listen(PORT, () => {
  const os = require('os');
  const nets = os.networkInterfaces();
  let lan = null;
  for (const name of Object.keys(nets))
    for (const n of nets[name] || [])
      if (n.family === 'IPv4' && !n.internal) { lan = n.address; break; }
  console.log('=================================================');
  console.log(`SwingSense AI running at http://localhost:${PORT}`);
  if (lan) console.log(`On your phone (same Wi-Fi): http://${lan}:${PORT}`);
  console.log('Daily auto-scan scheduled for 6:40 PM (Mon-Fri)');
  console.log('=================================================');
});
