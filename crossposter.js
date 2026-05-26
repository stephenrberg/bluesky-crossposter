const puppeteer = require('/usr/local/lib/node_modules/puppeteer');
const fs = require('fs');
const path = require('path');

(async () => {
  // Parse data package sent from Python
  const inputData = JSON.parse(process.argv[2]);
  const { text, reply_id, quote_id, media } = inputData;

  const browser = await puppeteer.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
  });

  const page = await browser.newPage();
  await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');

  // Helper function to cleanly capture tokens before exiting a branch
  async function saveSessionCookies() {
    try {
      const updatedCookies = await page.cookies();
      fs.writeFileSync('./cookies.json', JSON.stringify(updatedCookies, null, 2));
      console.error("Session cookies refreshed and saved to disk.");
    } catch (err) {
      console.error("Failed to write updated session cookies:", err);
    }
  }

  let interceptedTweetId = null;

  // *** STRATEGY UNIFICATION ***
  // Declaring the network interceptor globally right here allows it to watch
  // background traffic for BOTH replies and standalone homepage posts seamlessly.
  page.on('response', async (response) => {
    const url = response.url();
    if (url.includes('CreateTweet')) {
      try {
        const textData = await response.text();
        const parsed = JSON.parse(textData);
        
        // Drill down to extract the absolute numeric resource ID string
        const tweetId = parsed?.data?.create_tweet?.tweet_results?.result?.rest_id;
        if (tweetId) {
          console.error(`[TRAFFIC INTERCEPT]: Successfully snared live Tweet ID from API: ${tweetId}`);
          interceptedTweetId = String(tweetId);
        }
      } catch (err) {
        // Fail silently if packet was pre-flight validation traffic
      }
    }
  });

  try {
    // 1. Authenticate using cookies
    const cookies = JSON.parse(fs.readFileSync('./cookies.json', 'utf8'));
    await page.setCookie(...cookies);

    // ==========================================
    // BRANCH A: DEEP THREAD REPLY ENGINE
    // ==========================================
    if (reply_id) {
        console.error(`Navigating to target parent thread status: ${reply_id}`);
        await page.goto(`https://x.com/i/status/${reply_id}`, { waitUntil: 'networkidle2' });

        let completeText = text;
        if (quote_id) {
          completeText += ` \nhttps://x.com/i/status/${quote_id}`;
        }

        console.error("Direct tweet layout detected. Initializing reply editor activation flow...");
        
        const placeholderSelector = '[data-testid="inlineReply_entry_container"], div[role="progressbar"] + div';
        await page.waitForSelector(`${placeholderSelector}, [data-testid="tweetTextarea_0"], [data-testid="tweetTextarea_0R"]`, { timeout: 15000 });
        
        const placeholder = await page.$(placeholderSelector);
        if (placeholder) {
            console.error("Found reply placeholder element container. Executing initialization click...");
            await placeholder.click();
            await new Promise(resolve => setTimeout(resolve, 2000)); 
        }
        
        let editorSelector = '';
        if (await page.$('[data-testid="tweetTextarea_0R"]')) {
            editorSelector = '[data-testid="tweetTextarea_0R"]';
        } else {
            editorSelector = '[data-testid="tweetTextarea_0"]';
        }
        
        console.error(`Targeting initialized text box: ${editorSelector}`);
        await page.waitForSelector(editorSelector, { timeout: 15000 });
        await page.focus(editorSelector);
        
        await page.click(editorSelector, { clickCount: 3 });
        await page.keyboard.press('Backspace');
        await page.keyboard.type(completeText, { delay: 100 });
        await new Promise(resolve => setTimeout(resolve, 2000));

        if (media && media.length > 0) {
          const fileInputSelector = 'input[data-testid="fileInput"]';
          await page.waitForSelector(fileInputSelector);
          const fileInput = await page.$(fileInputSelector);
          const absolutePaths = media.map(p => path.resolve(p));
          await fileInput.uploadFile(...absolutePaths);
          await page.waitForSelector('[data-testid="attachments"]', { timeout: 20000 });
        }

        await page.waitForSelector('[data-testid="tweetButton"], [data-testid="tweetButtonInline"]', { timeout: 15000 });
        
        let postButtonSelector = await page.evaluate(() => {
          const inlineBtn = document.querySelector('[data-testid="tweetButtonInline"]');
          if (inlineBtn && inlineBtn.getBoundingClientRect().width > 0) {
              return '[data-testid="tweetButtonInline"]';
          }
          return '[data-testid="tweetButton"]';
        });

        console.error(`Targeting active reply submission selector: ${postButtonSelector}`);
        await page.evaluate((selector) => {
          const btn = document.querySelector(selector);
          if (btn) {
              btn.removeAttribute('aria-disabled'); 
              btn.click();
          }
        }, postButtonSelector);

        console.error("Reply submitted. Waiting for backend API tracking receipt tokens...");
        
        // Wait loop monitoring our network traffic intercept parameter
        for (let check = 0; check < 20; check++) {
          if (interceptedTweetId) break;
          await new Promise(resolve => setTimeout(resolve, 500));
        }

        await saveSessionCookies();

        if (interceptedTweetId) {
          console.log(interceptedTweetId);
        } else {
          console.log(`fallback_${Date.now()}`);
        }
    } 
    // ==========================================
    // BRANCH B: STANDARD HOMEPAGE ROOT ENGINE
    // ==========================================
    else {
        await page.goto('https://x.com/home', { waitUntil: 'domcontentloaded' });
        
        const editorSelector = '[data-testid="tweetTextarea_0"]';
        
        let completeText = text;
        if (quote_id) {
          completeText += ` \nhttps://x.com/i/status/${quote_id}`;
        }
        
        await page.waitForSelector(editorSelector, { timeout: 15000 });
        await page.focus(editorSelector);
        
        await page.click(editorSelector, { clickCount: 3 });
        await page.keyboard.press('Backspace');
        await page.keyboard.type(completeText, { delay: 100 });
        await new Promise(resolve => setTimeout(resolve, 2000));

        if (media && media.length > 0) {
          const fileInputSelector = 'input[data-testid="fileInput"]';
          await page.waitForSelector(fileInputSelector);
          const fileInput = await page.$(fileInputSelector);
          const absolutePaths = media.map(p => path.resolve(p));
          await fileInput.uploadFile(...absolutePaths);
          await page.waitForSelector('[data-testid="attachments"]', { timeout: 20000 });
        }

        const postButtonSelector = '[data-testid="tweetButtonInline"]';
        await page.waitForSelector(postButtonSelector, { timeout: 15000 });

        await page.evaluate((selector) => {
          const btn = document.querySelector(selector);
          if (btn) {
              btn.removeAttribute('aria-disabled'); 
              btn.click();
          }
        }, postButtonSelector);

        console.error("Standard home post submitted. Waiting for backend API tracking receipt tokens...");
        
        // *** UPGRADE FOR HOMEPAGE: Monitor the exact same network token parameters ***
        for (let check = 0; check < 20; check++) {
          if (interceptedTweetId) break;
          await new Promise(resolve => setTimeout(resolve, 500));
        }

        await saveSessionCookies();

        if (interceptedTweetId) {
            console.log(interceptedTweetId);
        } else {
            console.log(`fallback_${Date.now()}`);
        }
    }

  } catch (error) {
    console.error('Browser interaction breakdown:', error);

    try {
      const currentUrl = page.url();
      if (currentUrl && currentUrl !== 'about:blank') {
        const updatedCookies = await page.cookies();
        fs.writeFileSync('./cookies.json', JSON.stringify(updatedCookies, null, 2));
        console.error("Emergency cookie state saved during crash recovery.");
      }
    } catch (cookieErr) {
      console.error("Could not rescue cookies during crash:", cookieErr.message);
    }
    
    process.exit(1);
  } finally {
    await browser.close();
  }
})();