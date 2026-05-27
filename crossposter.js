const puppeteer = require('/usr/local/lib/node_modules/puppeteer');
const fs = require('fs');
const path = require('path');

(async () => {
  const inputData = JSON.parse(process.argv[2]);
  const { text, reply_id, quote_id, media } = inputData;

  const browser = await puppeteer.launch({
    headless: true,
    args: [
      '--no-sandbox', 
      '--disable-setuid-sandbox', 
      '--disable-dev-shm-usage',        // *** CRITICAL OOM FIX: Avoids temporary partition memory drops ***
      '--disable-gpu', 
      '--disable-software-rasterizer',
      '--mute-audio',
      // *** NEW MEMORY ENGINE MANAGEMENT FLAGS ***
      '--js-flags="--max-old-space-size=512"', // Strict limit constraints on active V8 memory footprint
      '--memory-pressure-threshold-ms=1',    // Forces real-time system garbage collection sweeps
      '--no-zygote',
      '--no-first-run'
    ]
  });

  const page = await browser.newPage();
  await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');

  let interceptedTweetId = null;
  page.on('response', async (response) => {
    const url = response.url();
    if (url.includes('CreateTweet') && response.request().method() === 'POST') {
      try {
        const textData = await response.text();
        const parsed = JSON.parse(textData);
        interceptedTweetId = parsed?.data?.create_tweet?.tweet_results?.result?.rest_id;
      } catch (err) {}
    }
  });

  try {
    const cookies = JSON.parse(fs.readFileSync('./cookies.json', 'utf8'));
    await page.setCookie(...cookies);

    // ==========================================
    // BRANCH A: DEEP THREAD REPLY ENGINE
    // ==========================================
    if (reply_id) {
        await page.goto(`https://x.com/i/status/${reply_id}`, { waitUntil: 'networkidle2' });
        let completeText = text + (quote_id ? ` \nhttps://x.com/i/status/${quote_id}` : '');

        const placeholderSelector = '[data-testid="inlineReply_entry_container"], div[role="progressbar"] + div';
        await page.waitForSelector(`${placeholderSelector}, [data-testid="tweetTextarea_0"]`, { timeout: 15000 });
        
        if (await page.$(placeholderSelector)) {
            await page.click(placeholderSelector);
            await new Promise(resolve => setTimeout(resolve, 1500));
        }
        
        const editorSelector = (await page.$('[data-testid="tweetTextarea_0R"]')) ? '[data-testid="tweetTextarea_0R"]' : '[data-testid="tweetTextarea_0"]';
        await page.waitForSelector(editorSelector);
        await page.focus(editorSelector);
        await page.keyboard.type(completeText, { delay: 50 });

        if (media && media.length > 0) {
          const fileInputSelector = 'input[data-testid="fileInput"]';
          await page.waitForSelector(fileInputSelector);
          const fileInput = await page.$(fileInputSelector);
          const absolutePaths = media.map(p => path.resolve(p));
          
          console.error("Attaching media file packages to composition frame...");
          await fileInput.uploadFile(...absolutePaths);
          
          const hasVideo = absolutePaths.some(p => p.toLowerCase().endsWith('.mp4') || p.toLowerCase().endsWith('.mov'));
          if (hasVideo) {
             console.error("Video detected. Holding execution window for 45 seconds to guarantee background data chunks transfer safely...");
             await new Promise(resolve => setTimeout(resolve, 45000)); 
          } else {
             await page.waitForSelector('[data-testid="attachments"]', { timeout: 15000 });
             await new Promise(resolve => setTimeout(resolve, 2000));
          }
        }

        await page.evaluate(() => {
          const btn = document.querySelector('[data-testid="tweetButtonInline"]') || document.querySelector('[data-testid="tweetButton"]');
          if (btn) {
              btn.removeAttribute('aria-disabled');
              btn.click();
          }
        });
    } 
    // ==========================================
    // BRANCH B: STANDARD HOMEPAGE ROOT ENGINE
    // ==========================================
    else {
        await page.goto('https://x.com/home', { waitUntil: 'domcontentloaded' });
        const editorSelector = '[data-testid="tweetTextarea_0"]';
        await page.waitForSelector(editorSelector);
        await page.focus(editorSelector);
        await page.keyboard.type(text + (quote_id ? ` \nhttps://x.com/i/status/${quote_id}` : ''), { delay: 50 });

        if (media && media.length > 0) {
          const fileInputSelector = 'input[data-testid="fileInput"]';
          await page.waitForSelector(fileInputSelector);
          const fileInput = await page.$(fileInputSelector);
          const absolutePaths = media.map(p => path.resolve(p));
          
          console.error("Attaching media file packages to composition frame...");
          await fileInput.uploadFile(...absolutePaths);
          
          const hasVideo = absolutePaths.some(p => p.toLowerCase().endsWith('.mp4') || p.toLowerCase().endsWith('.mov'));
          if (hasVideo) {
             console.error("Video detected. Holding execution window for 45 seconds to guarantee background data chunks transfer safely...");
             await new Promise(resolve => setTimeout(resolve, 45000));
          } else {
             await page.waitForSelector('[data-testid="attachments"]', { timeout: 15000 });
             await new Promise(resolve => setTimeout(resolve, 2000));
          }
        }

        await page.evaluate(() => {
          const btn = document.querySelector('[data-testid="tweetButtonInline"]') || document.querySelector('[data-testid="tweetButton"]');
          if (btn) {
              btn.removeAttribute('aria-disabled');
              btn.click();
          }
        });
    }

    for (let check = 0; check < 40; check++) {
      if (interceptedTweetId) break;
      await new Promise(resolve => setTimeout(resolve, 500));
    }

    const updatedCookies = await page.cookies();
    fs.writeFileSync('./cookies.json', JSON.stringify(updatedCookies, null, 2));

    console.log(interceptedTweetId || `fallback_${Date.now()}`);

  } catch (error) {
    console.error('Execution failure:', error);
    process.exit(1);
  } finally {
    await browser.close();
  }
})();