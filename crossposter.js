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

  try {
    // 1. Authenticate using cookies
    const cookies = JSON.parse(fs.readFileSync('./cookies.json', 'utf8'));
    await page.setCookie(...cookies);

    // 2. Direct routing depending on whether it's a root post or a thread reply
    if (reply_id) {
        // 2. Direct routing depending on whether it's a root post or a thread reply
        await page.goto(`https://x.com/i/status/${reply_id}`, { waitUntil: 'networkidle2' });

        // Prepare complete text block string context before dealing with inputs
        let completeText = text;
        if (quote_id) {
        completeText += ` \nhttps://x.com/i/status/${quote_id}`;
        }

        // 3. Handle Text Entry Selector Focus Engine
        let editorSelector = '';

        console.error("Direct tweet layout detected. Initializing reply editor activation flow...");
        
        // Selectors used for the interactive outer placeholder wrappers
        const placeholderSelector = '[data-testid="inlineReply_entry_container"], div[role="progressbar"] + div';
        
        // Wait for either the active editor or the dormant placeholder box to appear
        await page.waitForSelector(`${placeholderSelector}, [data-testid="tweetTextarea_0"], [data-testid="tweetTextarea_0R"]`, { timeout: 15000 });
        
        // If the entry placeholder box layer is found, click it to force React to render the actual text box
        const placeholder = await page.$(placeholderSelector);
        if (placeholder) {
            console.error("Found reply placeholder element container. Executing initialization click...");
            await placeholder.click();
            await new Promise(resolve => setTimeout(resolve, 1500)); // Sleep 1.5s to let the animation expand
        }
        
        // Dynamic evaluation check: track down which variation React dropped into the DOM
        if (await page.$('[data-testid="tweetTextarea_0R"]')) {
            editorSelector = '[data-testid="tweetTextarea_0R"]';
        } else {
            editorSelector = '[data-testid="tweetTextarea_0"]';
        }
        
        console.error(`Targeting initialized text container selector: ${editorSelector}`);
        await page.waitForSelector(editorSelector, { timeout: 15000 });
        await page.focus(editorSelector);
        
        // Clear any default or placeholder text just in case
        await page.click(editorSelector, { clickCount: 3 });
        await page.keyboard.press('Backspace');

        // Type text mimicking a human typing speed
        await page.keyboard.type(completeText, { delay: 100 });
        
        // Give Twitter's React backend a brief moment to sync the typed state
        await new Promise(resolve => setTimeout(resolve, 2000));

        // 4. File uploads
        if (media && media.length > 0) {
        const fileInputSelector = 'input[data-testid="fileInput"]';
        await page.waitForSelector(fileInputSelector);
        const fileInput = await page.$(fileInputSelector);
        
        const absolutePaths = media.map(p => path.resolve(p));
        await fileInput.uploadFile(...absolutePaths);
        
        await page.waitForSelector('[data-testid="attachments"]', { timeout: 20000 });
        }

        // 5. Submit Post
        await page.waitForSelector('[data-testid="tweetButton"], [data-testid="tweetButtonInline"]', { timeout: 15000 });
        
        // Evaluate visibility in the live DOM context to ensure we pick the interactive button
        let postButtonSelector = await page.evaluate(() => {
        const inlineBtn = document.querySelector('[data-testid="tweetButtonInline"]');
        if (inlineBtn && inlineBtn.getBoundingClientRect().width > 0) {
            return '[data-testid="tweetButtonInline"]';
        }
        return '[data-testid="tweetButton"]';
        });

        console.error(`Targeting active button selector: ${postButtonSelector}`);

        // Force a JavaScript click event evaluation to override headless element blocks
        await page.evaluate((selector) => {
        const btn = document.querySelector(selector);
        if (btn) {
            btn.removeAttribute('aria-disabled'); // Strip protection block if active
            btn.click();
        }
        }, postButtonSelector);

        // 6. Sniff out the new Tweet ID from the redirected/network window response
        console.error("Post submitted. Monitoring state for dynamic URL modifications...");
        
        let finalTweetId = null;
        
        // Check up to 16 times (8 seconds total) for the updated resource ID
        for (let check = 0; check < 16; check++) {
        // Check 1: Address Bar URL Regex Match
        const currentUrl = page.url();
        const idMatch = currentUrl.match(/status(?:es)?\/(\d+)/);
        
        if (idMatch && idMatch[1] && idMatch[1] !== reply_id) {
            finalTweetId = idMatch[1];
            break;
        }
        
        // Check 2: Scrape the DOM timeline elements for the newest status anchor link
        finalTweetId = await page.evaluate((parentReplyId) => {
            const links = Array.from(document.querySelectorAll('a[href*="/status/"]'));
            for (let link of links) {
            const match = link.href.match(/status\/(\d+)/);
            if (match && match[1] !== parentReplyId) {
                return match[1];
            }
            }
            return null;
        }, reply_id || null);
        
        if (finalTweetId) break;
        
        // Pause 500ms before executing next tracking pass iteration
        await new Promise(resolve => setTimeout(resolve, 500));
        }

        // Final Payload Handoff back to Python stdout stream
        if (finalTweetId) {
        console.log(finalTweetId);
        } else {
        console.log(`fallback_${Date.now()}`);
        }
    } else {
        await page.goto('https://x.com/home', { waitUntil: 'networkidle2' });
        
        // 3. Handle Text Entry Selector
        const editorSelector = reply_id ? '[data-testid="tweetTextarea_0R"]' : '[data-testid="tweetTextarea_0"]';
        
        let completeText = text;
        if (quote_id) {
        completeText += ` \nhttps://x.com/i/status/${quote_id}`;
        }
        
        await page.waitForSelector(editorSelector, { timeout: 15000 });
        await page.focus(editorSelector);
        
        // Clear any default or placeholder text just in case
        await page.click(editorSelector, { clickCount: 3 });
        await page.keyboard.press('Backspace');

        // Type text mimicking a human typing speed
        await page.keyboard.type(completeText, { delay: 100 });
        
        // Give Twitter's React backend a brief moment to sync the typed state
        await new Promise(resolve => setTimeout(resolve, 2000));

        // 4. File uploads
        if (media && media.length > 0) {
        const fileInputSelector = 'input[data-testid="fileInput"]';
        await page.waitForSelector(fileInputSelector);
        const fileInput = await page.$(fileInputSelector);
        
        const absolutePaths = media.map(p => path.resolve(p));
        await fileInput.uploadFile(...absolutePaths);
        
        await page.waitForSelector('[data-testid="attachments"]', { timeout: 20000 });
        }

        // 5. Submit Post
        const postButtonSelector = reply_id ? '[data-testid="tweetButton"]' : '[data-testid="tweetButtonInline"]';
        await page.waitForSelector(postButtonSelector);

        // Force a JavaScript click event evaluation to override headless element blocks
        await page.evaluate((selector) => {
        const btn = document.querySelector(selector);
        if (btn) {
            btn.removeAttribute('aria-disabled'); // Strip protection block if active
            btn.click();
        }
        }, postButtonSelector);

        // 6. Sniff out the new Tweet ID from the redirected/network window response
        // Wait for the native navigation redirect event
        await page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 10000 }).catch(() => {});
        
        await new Promise(resolve => setTimeout(resolve, 3000));
        
        const currentUrl = page.url();
        const idMatch = currentUrl.match(/status(?:es)?\/(\d+)/);
        
        if (idMatch && idMatch[1]) {
            // Success! Print the real numeric ID back to Python
            console.log(idMatch[1]);
        } else {
            // If we are still on /home or /status/reply_id, look at the DOM for the newest tweet link
            const fallbackId = await page.evaluate(() => {
                const links = Array.from(document.querySelectorAll('a[href*="/status/"]'));
                for (let link of links) {
                const match = link.href.match(/status\/(\d+)/);
                if (match) return match[1];
                }
                return null;
            });

            if (fallbackId) {
                console.log(fallbackId);
            } else {
                // Ultimate fallback if the browser completely lost its tracking context
                console.log(`fallback_${Date.now()}`);
            }
        }
    }

  } catch (error) {
    console.error('Browser interaction breakdown:', error);
    process.exit(1);
  } finally {
    await browser.close();
  }
})();