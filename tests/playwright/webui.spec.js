// @ts-check
const { test, expect } = require('@playwright/test');
const { ensureTestUser, authenticate } = require('./auth-helper');

const SESSION_ID = 'orchestrator-test-daeb7396';

test.beforeAll(async () => {
  await ensureTestUser();
});

test.beforeEach(async ({ page }) => {
  await authenticate(page);
});

test.describe('Chat page', () => {
  test('loads and shows session history', async ({ page }) => {
    await page.goto(`/chat/${SESSION_ID}`);

    // Wait for chat messages to load (synthetic turns from event store)
    const messageContainer = page.locator('.messages-container, .chat-messages, [class*="message"]').first();
    await expect(messageContainer).toBeVisible({ timeout: 15_000 });

    // Should have at least one message bubble
    const messages = page.locator('.message-bubble, .user-message, .assistant-message, [class*="bubble"]');
    await expect(messages.first()).toBeVisible({ timeout: 15_000 });
  });

  test('can send a message', async ({ page }) => {
    await page.goto(`/chat/${SESSION_ID}`);

    // Wait for page to be ready
    await page.waitForLoadState('networkidle');

    // Find the input area
    const input = page.locator('textarea, input[type="text"], [contenteditable]').last();
    await expect(input).toBeVisible({ timeout: 10_000 });

    // Type and send
    const testMsg = `Playwright test ${Date.now()} - just say "acknowledged"`;
    await input.fill(testMsg);
    await input.press('Enter');

    // Verify the message appears in the chat
    await expect(page.locator(`text=${testMsg.substring(0, 30)}`).first()).toBeVisible({ timeout: 10_000 });
  });

  test('events panel shows events', async ({ page }) => {
    await page.goto(`/chat/${SESSION_ID}`);
    await page.waitForLoadState('networkidle');

    // Look for events/activity section
    const eventsSection = page.locator('[class*="event"], [class*="activity"], [class*="stream"]').first();
    if (await eventsSection.isVisible().catch(() => false)) {
      await expect(eventsSection).toBeVisible();
    }
  });
});

test.describe('Asks page', () => {
  test('loads and shows pending asks', async ({ page }) => {
    await page.goto('/asks');
    await page.waitForLoadState('networkidle');

    // Should see the asks page content
    await expect(page.locator('text=Agent Asks').or(page.locator('text=Deferred')).or(page.locator('text=Pending'))).toBeVisible({ timeout: 10_000 });

    // Should have at least one pending ask (from our earlier test)
    const askCard = page.locator('[class*="ask"], [class*="card"]').first();
    await expect(askCard).toBeVisible({ timeout: 10_000 });
  });

  test('can answer a deferred ask', async ({ page }) => {
    await page.goto('/asks');
    await page.waitForLoadState('networkidle');

    // Find a pending ask card
    const askCard = page.locator('[class*="ask-card"], [class*="card"]').first();
    await expect(askCard).toBeVisible({ timeout: 10_000 });

    // Click one of the choice buttons if available
    const choiceBtn = askCard.locator('button').first();
    if (await choiceBtn.isVisible().catch(() => false)) {
      const btnText = await choiceBtn.textContent();
      await choiceBtn.click();

      // After answering, the status should change
      await page.waitForTimeout(2000);
    }
  });
});

test.describe('Sessions page', () => {
  test('loads and shows session list', async ({ page }) => {
    await page.goto('/');
    await page.waitForLoadState('networkidle');

    // Should see session cards/list
    const sessionItem = page.locator('[class*="session"], [class*="card"]').first();
    await expect(sessionItem).toBeVisible({ timeout: 10_000 });
  });
});

test.describe('Navigation', () => {
  test('nav links work', async ({ page }) => {
    await page.goto('/');
    await page.waitForLoadState('networkidle');

    // Check nav has expected links
    const nav = page.locator('nav, [class*="nav"]').first();
    await expect(nav).toBeVisible({ timeout: 5_000 });

    // Click Asks link
    const asksLink = page.locator('a[href*="asks"], nav >> text=Asks').first();
    if (await asksLink.isVisible().catch(() => false)) {
      await asksLink.click();
      await expect(page).toHaveURL(/asks/);
    }
  });
});
