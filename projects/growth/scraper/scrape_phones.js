#!/usr/bin/env node
/**
 * Playwright scraper to extract agent phone numbers from Imovirtual listing pages.
 *
 * Reads leads_idealista.csv, visits listing pages, reveals phone numbers, and updates the CSV.
 * Resumable: skips rows that already have a phone number.
 *
 * Usage: node scrape_phones.js
 */

const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const { parse } = require('csv-parse/sync');
const { stringify } = require('csv-stringify/sync');

const CSV_PATH = path.resolve(__dirname, '../data/leads_idealista.csv');
const DELAY_MS = 1500; // 1.5s between requests

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

function extractSlugFromNotes(notes) {
  // Notes contain "Listing: https://www.imovirtual.com/pt/ad/{slug}/"
  const match = notes && notes.match(/Listing:\s*https?:\/\/www\.imovirtual\.com\/pt\/(?:ad|anuncio)\/([^/\s]+)/);
  if (match) return match[1];
  return null;
}

function normalizePhone(raw) {
  const digits = raw.replace(/\D/g, '');
  // Handle +351 / 00351 prefix
  if (digits.length === 12 && digits.startsWith('351')) return digits.slice(3);
  if (digits.length === 13 && digits.startsWith('00351')) return digits.slice(5);
  if (digits.length === 9) return digits;
  return digits;
}

async function dismissCookies(page) {
  // Remove OneTrust overlay via JS (faster and more reliable than clicking)
  await page.evaluate(() => {
    const sdk = document.getElementById('onetrust-consent-sdk');
    if (sdk) sdk.remove();
    const filter = document.querySelector('.onetrust-pc-dark-filter');
    if (filter) filter.remove();
    // Also remove any backdrop/overlay elements
    document.querySelectorAll('[class*="overlay"], [class*="backdrop"]').forEach(el => {
      if (el.style.position === 'fixed' || el.style.zIndex > 100) el.remove();
    });
  });
}

async function extractPhoneFromListing(page, slug) {
  const url = `https://www.imovirtual.com/pt/anuncio/${slug}`;

  try {
    // networkidle ensures JS is fully loaded so the phone reveal button works
    const response = await page.goto(url, { waitUntil: 'networkidle', timeout: 30000 });

    // Check for 404
    const title = await page.title();
    if (title.includes('404') || title.toLowerCase().includes('erro 404') || (response && response.status() === 404)) {
      return { phone: null, error: '404' };
    }

    // Remove cookie/consent overlay
    await dismissCookies(page);

    // Find and click phone reveal button
    const phoneBtn = await page.$('[data-cy="phone-number.show-full-number-button"]');
    if (phoneBtn) {
      await phoneBtn.click({ force: true });
      await sleep(3000);
    }

    // Extract phone numbers from tel: links
    const telLinks = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('a[href^="tel:"]'))
        .map(a => a.getAttribute('href').replace('tel:', '').trim())
        .filter(p => p.length > 0);
    });

    if (telLinks.length > 0) {
      const normalized = normalizePhone(telLinks[0]);
      return { phone: normalized, error: null };
    }

    return { phone: null, error: 'no_phone_found' };
  } catch (err) {
    return { phone: null, error: err.message.substring(0, 100) };
  }
}

async function main() {
  // Read CSV
  const rawCsv = fs.readFileSync(CSV_PATH, 'utf8');
  const rows = parse(rawCsv, { columns: true, skip_empty_lines: true });
  const columns = Object.keys(rows[0]);

  console.log(`Loaded ${rows.length} rows from CSV`);

  const todo = rows.filter(r => r.status === 'todo_phone' && !r.phone);
  const alreadyDone = rows.filter(r => r.phone && r.phone.length > 0).length;
  console.log(`Already have phone: ${alreadyDone}, To process: ${todo.length}`);

  if (todo.length === 0) {
    console.log('Nothing to process!');
    return;
  }

  // Launch browser
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    viewport: { width: 1280, height: 800 },
    locale: 'pt-PT',
  });
  const page = await context.newPage();

  // Track progress
  let processed = 0;
  let found = 0;
  let failed = 0;
  let is404 = 0;
  const phonesSeen = new Set();

  // Pre-populate seen phones from already-done rows
  rows.forEach(r => { if (r.phone) phonesSeen.add(r.phone); });

  for (let i = 0; i < rows.length; i++) {
    const row = rows[i];

    // Skip if already has phone
    if (row.phone && row.phone.length > 0) continue;
    // Skip if not todo_phone
    if (row.status !== 'todo_phone') continue;

    const slug = extractSlugFromNotes(row.notes);
    if (!slug) {
      console.log(`[${processed + 1}/${todo.length}] ${row.name}: No listing URL in notes, skipping`);
      processed++;
      failed++;
      continue;
    }

    processed++;
    const label = `[${processed}/${todo.length}]`;
    process.stdout.write(`${label} ${row.name.substring(0, 40)} ... `);

    const { phone, error } = await extractPhoneFromListing(page, slug);

    if (phone) {
      const isDuplicate = phonesSeen.has(phone);
      if (!isDuplicate) {
        phonesSeen.add(phone);
        found++;
      }
      rows[i].phone = phone;
      rows[i].status = 'new';
      console.log(`✓ ${phone}${isDuplicate ? ' (dup)' : ''}`);
    } else {
      if (error === '404') {
        is404++;
        console.log(`✗ listing removed`);
      } else {
        console.log(`✗ ${error || 'no phone'}`);
      }
      failed++;
    }

    // Save progress after each row
    const updatedCsv = stringify(rows, { header: true, columns });
    fs.writeFileSync(CSV_PATH, updatedCsv);

    // Rate limit
    await sleep(DELAY_MS);
  }

  await browser.close();

  console.log('\n=== Summary ===');
  console.log(`Processed: ${processed}`);
  console.log(`Found phones: ${found} unique`);
  console.log(`404 (removed listings): ${is404}`);
  console.log(`Failed (no phone): ${failed - is404}`);
  console.log(`Total rows with phone now: ${rows.filter(r => r.phone).length}`);
}

main().catch(err => {
  console.error('Fatal error:', err);
  process.exit(1);
});
