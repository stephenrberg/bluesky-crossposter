const puppeteer = require('/usr/local/lib/node_modules/puppeteer');
const fs = require('fs');
const path = require('path');
const os = require('os');

(async () => {
  const inputData = JSON.parse(process.argv[2]);
  let { text, reply_id, quote_id, media, embed_url, bsky_thumb_url } = inputData;

  const browser = await puppeteer.launch({
    headless: true,
    args: [
      '--no-sandbox', 
      '--disable-setuid-sandbox', 
      '--disable-dev-shm-usage',        // *** CRITICAL OOM FIX: Avoids temporary partition memory drops ***
      '--disable-gpu', 
      '--disable-software-rasterizer',
      '--mute-audio',
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

  let tempDownloadedFile = null;

  try {
    const rawCookies = JSON.parse(fs.readFileSync('./cookies.json', 'utf8'));
    
    const cookies = rawCookies.map(cookie => {
      if (cookie.partitionKey && typeof cookie.partitionKey === 'string') {
        delete cookie.partitionKey;
      }
      return cookie;
    });

    await page.setCookie(...cookies);

    // --- DOWNLOAD PROXIED IMAGE WORKAROUND ---
    if ((!media || media.length === 0) && embed_url && bsky_thumb_url) {
      const lowerUrl = embed_url.toLowerCase();
      if (lowerUrl.includes('serializd.com') || lowerUrl.includes('goodreads.com')) {
        console.error(`Broken card wrapper targeted (${embed_url}). Triggering image asset injection proxy...`);
        try {
          const response = await fetch(bsky_thumb_url);
          if (response.ok) {
            const buffer = Buffer.from(await response.arrayBuffer());
            const tempPath = path.join(os.tmpdir(), `proxy_card_${Date.now()}.jpg`);
            fs.writeFileSync(tempPath, buffer);
            tempDownloadedFile = tempPath;
            
            // Explicitly force media array assignment up front
            media = [tempPath];
            console.error(`Staged local backup file layout mirror at: ${tempPath}`);
          }
        } catch (fetchErr) {
          console.error('Failed to download card proxy preview thumbnail:', fetchErr);
        }
      }
    }

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

        // MEDIA ATTACHMENT LOOP (REPLY)
        if (media && media.length > 0) {
          const fileInputSelector = 'input[type="file"], input[data-testid="fileInput"]';
          await page.waitForSelector(fileInputSelector, { timeout: 15000 });
          const fileInput = await page.$(fileInputSelector);
          const absolutePaths = media.map(p => path.resolve(p));
          
          console.error("Attaching media file packages to reply frame:", absolutePaths);
          await fileInput.uploadFile(...absolutePaths); // Injects file stream context
          
          // Force a state update listener change event inside Twitter's DOM structure
          await fileInput.evaluate(upload => upload.dispatchEvent(new Event('change', { bubbles: true })));
          
          const hasVideo = absolutePaths.some(p => p.toLowerCase().endsWith('.mp4') || p.toLowerCase().endsWith('.mov'));
          if (hasVideo) {
             console.error("Video detected. Holding execution window for 45 seconds to guarantee background data chunks transfer safely...");
             await new Promise(resolve => setTimeout(resolve, 45000)); 
          } else {
             await page.waitForSelector('[data-testid="attachments"]', { timeout: 15000 });
             await new Promise(resolve => setTimeout(resolve, 3000));
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
    else {
        await page.goto('https://x.com/home', { waitUntil: 'domcontentloaded' });
        const editorSelector = '[data-testid="tweetTextarea_0"]';
        await page.waitForSelector(editorSelector);
        await page.focus(editorSelector);
        await page.keyboard.type(text + (quote_id ? ` \nhttps://x.com/i/status/${quote_id}` : ''), { delay: 50 });

        // MEDIA ATTACHMENT LOOP (PRIMARY POST)
        if (media && media.length > 0) {
          const fileInputSelector = 'input[type="file"], input[data-testid="fileInput"]';
          await page.waitForSelector(fileInputSelector, { timeout: 15000 });
          const fileInput = await page.$(fileInputSelector);
          const absolutePaths = media.map(p => path.resolve(p));
          
          console.error("Attaching media file packages to primary compose frame:", absolutePaths);
          await fileInput.uploadFile(...absolutePaths); // Injects file stream context
          
          // Force a state update listener change event inside Twitter's DOM structure
          await fileInput.evaluate(upload => upload.dispatchEvent(new Event('change', { bubbles: true })));
          
          const hasVideo = absolutePaths.some(p => p.toLowerCase().endsWith('.mp4') || p.toLowerCase().endsWith('.mov'));
          if (hasVideo) {
             console.error("Video detected. Holding execution window for 45 seconds to guarantee background data chunks transfer safely...");
             await new Promise(resolve => setTimeout(resolve, 45000));
          } else {
             await page.waitForSelector('[data-testid="attachments"]', { timeout: 15000 });
             await new Promise(resolve => setTimeout(resolve, 3000));
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
    if (tempDownloadedFile && fs.existsSync(tempDownloadedFile)) {
      try {
        fs.unlinkSync(tempDownloadedFile);
        console.error('Cleaned up temporary proxy card file.');
      } catch (cleanupErr) {
        console.error('Failed to clean up temp file:', cleanupErr);
      }
    }
    await browser.close();
  }
})();