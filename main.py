import cloudscraper
from eth_account import Account
from eth_account.messages import encode_defunct
import aiohttp
import asyncio
import random
import logging
import config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger(__name__)

async def read_file(file_path):
    try:
        with open(file_path, 'r') as file:
            return [line.strip() for line in file if line.strip()]
    except FileNotFoundError:
        logger.error(f"File {file_path} not found")
        return []

async def captcha_solve(scraper, public):
    try:
        task = scraper.post(
            'https://api.capsolver.com/createTask',
            json={
                "clientKey": config.captcha_key,
                "task": {
                    "type": "ReCaptchaV2Task",
                    "websiteURL": "https://profile.bluwhale.com",
                    "websiteKey": "6LdcvNAqAAAAAPGIRpkc3LsBz_xFnyX5adFGHNx-",
                    "isInvisible": True,
                }
            },
        ).json()
        if task.get('errorId') != 0:
            logger.error(f"Captcha task creation failed: {task}")
            return None
        await asyncio.sleep(23)
        result = scraper.post('https://api.capsolver.com/getTaskResult', json={"clientKey": config.captcha_key,"taskId": task['taskId']},).json()
        if result.get('errorId') != 0:
            logger.error(f"[{public}] | Captcha task result failed: {result}")
            return None
        return result['solution']['gRecaptchaResponse']
    except Exception as ex:
        logger.error(f"[{public}] | Captcha solving error: {ex}")
        return None

async def registration(scraper, bearer, proxies, public):
    try:
        payload = {
            "token": bearer['access_token'],
            "user_type": "consumer",
            "auto_signup": True,
            "referral_code": config.referral_code
        }
        resp = scraper.post('https://ses.bluwhale.com/api/v1/auth/sign-in/', json=payload, proxies=proxies)
        resp_json = resp.json()
        logger.info(f"[{public}] | Registration: {resp_json['is_new_user']}")
        return True
    except Exception as ex:
        logger.error(f"[{public}] Registration error: {ex}")
        return False

async def check_in(scraper, bearer, proxies, public):
    try:
        check = scraper.get('https://ses.bluwhale.com/api/v1/wallets/check_sign_in_status/', headers={'authorization': f"Bearer {bearer['access_token']}"}, proxies=proxies).json()
        if check.get('signed_in_today'):
            logger.info(f'[{public}] | Already checked in')
        else:
            check_in = scraper.post('https://ses.bluwhale.com/api/v1/wallets/sign_in/',headers={'authorization': f"Bearer {bearer['access_token']}"},proxies=proxies).json()
            check = scraper.get('https://ses.bluwhale.com/api/v1/wallets/check_sign_in_status/',headers={'authorization': f"Bearer {bearer['access_token']}"},proxies=proxies).json()
            logger.info(f"[{public}] | Check-in: {check['signed_in_today']}")
    except Exception as ex:
        logger.error(f"[{public}] Check-in error: {ex}")

async def process_wallet(wallet, proxy, action, max_retries=5):
    scraper = cloudscraper.create_scraper()
    proxies = {"http": f"http://{proxy}", "https": f"http://{proxy}"} if proxy else None
    public = Account.from_key(wallet).address
    logger.info(f"[{public}] | Processing wallet with proxy: {proxy}")
    message = f'{{"address": "{public}", "chain_id": 56, "type": "signin"}}'
    for attempt in range(max_retries):
        try:
            captcha_token = await captcha_solve(scraper, public)
            if not captcha_token:
                logger.error(f"[{public}] CAPTCHA solving failed")
                return

            payload = {
                "message": message,
                "signature": '0x' + Account.sign_message(encode_defunct(text=message), wallet).signature.hex(),
                "address": public,
                "recaptcha_token": captcha_token
            }
            
            resp = scraper.post('https://ses.bluwhale.com/api/v1/auth/web3/sign-in/',json=payload,timeout=30,proxies=proxies).json()
            if 'token' not in resp:
                logger.warning(f"[{public}] Sign-in failed, attempt {attempt+1}/5. Response: {resp}")
                continue
            token = resp['token']
            
            token_resp = scraper.post('https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key=AIzaSyAt5pTGkbXQzw_VDIh8K_MXcJHBX3wgf_U', json={"token": token, "returnSecureToken": True},proxies=proxies).json()
            if 'refreshToken' not in token_resp:
                logger.error(f"[{public}] Token exchange failed. Response: {token_resp}")
                continue
            
            bearer = scraper.post('https://securetoken.googleapis.com/v1/token?key=AIzaSyAt5pTGkbXQzw_VDIh8K_MXcJHBX3wgf_U', data={'grant_type': 'refresh_token', 'refresh_token': token_resp["refreshToken"]}, proxies=proxies).json()
            if 'access_token' not in bearer:
                logger.error(f"[{public}] Bearer token failed. Response: {bearer}")
                continue
            
            if action == 1:
                success = await registration(scraper, bearer, proxies, public)
                await check_in(scraper, bearer, proxies, public)
            elif action == 2:
                await check_in(scraper, bearer, proxies, public)
            break
        except Exception as ex:
            logger.error(f'[{public or "Unknown"}] Attempt {attempt + 1}/{max_retries} Error | {ex}')
            if attempt == max_retries - 1:
                logger.error(f'[{public or "Unknown"}] Failed after {max_retries} attempts')
            await asyncio.sleep(2)

async def main():
    wallets = await read_file('data/privates.txt')
    proxies = await read_file('data/proxies.txt')
    
    if not wallets:
        logger.error("No wallets found. Exiting.")
        return
    
    try:
        action = int(input("Enter action (1 for registration, 2 for check-in): "))
        if action not in [1, 2]:
            raise ValueError("Invalid action")
    except ValueError:
        logger.error("Invalid action input. Using check-in (2) as default.")
        action = 2
    
    wallet_proxy_pairs = []
    for i, wallet in enumerate(wallets):
        proxy = proxies[i % len(proxies)] if proxies else None
        wallet_proxy_pairs.append((wallet, proxy))
    
    random.shuffle(wallet_proxy_pairs)
    
    try:
        num_threads = int(input("Enter number of threads: "))
        num_threads = max(1, min(num_threads, len(wallets)))
    except ValueError:
        logger.error("Invalid input. Using single thread.")
        num_threads = 1

    tasks = []
    for wallet, proxy in wallet_proxy_pairs:
        tasks.append(process_wallet(wallet, proxy, action))
    
    async with aiohttp.ClientSession() as session:
        for i in range(0, len(tasks), num_threads):
            batch = tasks[i:i + num_threads]
            await asyncio.gather(*[asyncio.create_task(task) for task in batch])

if __name__ == '__main__':
    asyncio.run(main())