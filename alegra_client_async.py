import asyncio
import os
from typing import Dict, List, Optional
from urllib.parse import quote
import logging

import aiohttp
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class AlegraClientAsync:
    def __init__(self, api_key=None, email=None, base_url=None, max_concurrent=5):
        self.base_url = base_url or os.getenv("ALEGRA_API_URL", "https://api.alegra.com/api/v1/")
        self.auth = aiohttp.BasicAuth(
            email or os.getenv("ALEGRA_EMAIL"),
            api_key or os.getenv("ALEGRA_API_KEY") or os.getenv("ALEGRA_TOKEN"),
        )
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=10, limit_per_host=10)
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(
                auth=self.auth, connector=connector, timeout=timeout
            )
        return self._session

    async def _request(self, method: str, endpoint: str, data: Optional[Dict] = None, retries: int = 3) -> Dict:
        session = await self._get_session()
        url = f"{self.base_url}{endpoint}"

        async with self._semaphore:
            for attempt in range(retries):
                try:
                    async with session.request(method, url, json=data) as resp:
                        if resp.status == 429:
                            wait = 2 ** attempt
                            logger.warning(f"Rate limit (429), esperando {wait}s")
                            await asyncio.sleep(wait)
                            continue
                        resp.raise_for_status()
                        return await resp.json()
                except aiohttp.ClientResponseError as e:
                    if e.status == 429 and attempt < retries - 1:
                        wait = 2 ** attempt
                        await asyncio.sleep(wait)
                        continue
                    logger.error(f"Alegra HTTP {e.status} en {method} {endpoint}")
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    if attempt < retries - 1:
                        wait = attempt + 1
                        logger.warning(f"Reintento {attempt + 1}/{retries} para {method} {endpoint}: {e}")
                        await asyncio.sleep(wait)
                        continue
                    logger.error(f"Error en {method} {endpoint}: {e}")
                    raise
        return {}

    async def get_invoices(self, page: int = 1, limit: int = 30) -> List[Dict]:
        endpoint = f"invoices?page={page}&limit={limit}"
        try:
            response = await self._request("GET", endpoint)
            return response if isinstance(response, list) else []
        except Exception:
            return []

    async def create_invoice(self, client_id: str, items: List[Dict], due_date: str,
                              date: Optional[str] = None, payment_form: str = "CASH",
                              payment_method: str = "CASH") -> Dict:
        data = {
            "client": client_id,
            "items": items,
            "dueDate": due_date,
            "status": "draft",
            "paymentForm": payment_form,
            "paymentMethod": payment_method,
        }
        if date:
            data["date"] = date
        return await self._request("POST", "invoices", data)

    async def get_clients(self, page: int = 1, limit: int = 30) -> List[Dict]:
        endpoint = f"contacts?page={page}&limit={limit}"
        try:
            response = await self._request("GET", endpoint)
            return response if isinstance(response, list) else []
        except Exception:
            return []

    def _clean_name(self, name: str) -> str:
        import re
        name = re.sub(r"\([^)]*\)", "", name)
        name = " ".join(name.split())
        return name.strip()

    def _score_client(self, client_name: str, search_tokens: set) -> int:
        import re
        client_tokens = set(re.sub(r"[()]", "", client_name).lower().split())
        return len(search_tokens & client_tokens)

    def _best_match(self, results: list, original_name: str):
        clean_tokens = set(self._clean_name(original_name).lower().split())
        if not clean_tokens:
            return results[0] if results else None
        best = None
        best_score = 0
        for client in results:
            score = self._score_client(client.get("name", ""), clean_tokens)
            if score > best_score:
                best_score = score
                best = client
        return best if best_score > 0 else (results[0] if results else None)

    async def search_client_by_name(self, name: str) -> Optional[Dict]:
        original = name
        clean = self._clean_name(name)
        words = clean.split()

        strategies = [original]
        if clean and clean != original:
            strategies.append(clean)
        if len(words) > 2:
            strategies.append(" ".join(words[:2]))
        if len(words) > 0:
            for s in strategies:
                if s == words[0]:
                    break
            else:
                strategies.append(words[0])

        for s in strategies:
            endpoint = f"contacts?name={quote(s)}&limit=10"
            try:
                response = await self._request("GET", endpoint)
                if isinstance(response, list) and response:
                    for client in response:
                        if client.get("name", "").lower() == original.lower():
                            return client
                    return self._best_match(response, original)
            except Exception as e:
                logger.warning("Error buscando cliente '%s' con estrategia '%s': %s", original, s, e)

        return None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
