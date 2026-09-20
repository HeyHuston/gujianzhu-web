from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

import asyncio

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import edge_tts  # Edge TTS（微软免费在线语音，中文音质好）

from config import DOCS_DIR, PROJECT_DIR
from rag import stream_answer
from poems_service import (
    get_building_entry,
    get_poem,
    get_poetry_landscape,
    get_stats,
    list_buildings,
    list_poems,
    list_poems_by_building,
    random_poem,
)


class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1)
    lang: str = "zh"
    history: List[Dict] = []
    mode: str = "chat"
    page_context: Dict = Field(default_factory=dict)


app = FastAPI(title="古建智寻 AI 导览 API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.post("/api/chat")
def chat(req: ChatRequest) -> StreamingResponse:
    print(f"[chat] 收到请求: query={req.query[:50]}... lang={req.lang} mode={req.mode}")

    def event_stream():
        try:
            for line in stream_answer(
                query=req.query,
                lang=req.lang,
                history=req.history,
                mode=req.mode,
                page_context=req.page_context,
            ):
                yield f"data: {line}\n\n"
        except Exception as exc:
            payload = {"type": "error", "data": str(exc)}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── 页面讲解稿（太和殿 / 御花园）───────────────────────────────────
_PAGE_SCRIPTS = {
    "taihe": {
        "zh": (
            "欢迎来到太和殿，俗称金銮殿，是北京故宫中轴线上的核心建筑。"
            "太和殿始建于明永乐十八年，即公元一四二零年，是明清两代皇帝举行重大典礼的场所。"
            "太和殿面阔十一间，进深五间，建筑面积约两千三百七十七平方米，是中国现存最大的木结构宫殿。"
            "大殿坐落在三层汉白玉须弥座台基之上，总高约八米，气势恢宏。"
            "殿顶为重檐庑殿顶，铺黄色琉璃瓦，这是中国古代建筑中等级最高的屋顶形式。"
            "殿内正中设有七层台阶的金漆雕龙宝座，宝座上方悬挂「建极绥猷」匾额。"
            "殿前有宽阔的丹陛，两侧陈列着日晷和嘉量，象征皇权天授和法度统一。"
            "太和殿前的太和门广场上，还摆放着铜铸的仙鹤、龟、鼎式香炉等，寓意江山永固。"
            "太和殿不仅是中国古代宫殿建筑的巅峰之作，更是中华文明的重要象征。"
        ),
        "en": (
            "Welcome to the Hall of Supreme Harmony, commonly known as the Golden Throne Hall. "
            "It is the central building on the central axis of the Forbidden City in Beijing. "
            "The Hall of Supreme Harmony was first built in 1420, the 18th year of the Yongle reign of the Ming Dynasty. "
            "It served as the venue for grand ceremonies held by emperors of the Ming and Qing dynasties. "
            "The hall is 11 bays wide and 5 bays deep, with a floor area of approximately 2,377 square meters, "
            "making it the largest surviving wooden palace structure in China. "
            "The hall stands on a three-tiered white marble platform with a total height of about 8 meters. "
            "Its roof is a double-eave hip roof covered with yellow glazed tiles, "
            "which is the highest-ranking roof form in traditional Chinese architecture. "
            "At the center of the hall is a gilded dragon throne on a seven-step platform, "
            "with a plaque reading 'Jian Ji Sui You' hanging above it. "
            "In front of the hall is a broad marble terrace, flanked by a sundial and a grain measure, "
            "symbolizing the heavenly mandate and the unity of law. "
            "The Hall of Supreme Harmony is not only the pinnacle of Chinese palace architecture, "
            "but also a powerful symbol of Chinese civilization."
        ),
    },
    "yuhuayuan": {
        "zh": (
            "欢迎来到御花园，位于北京故宫中轴线的最北端，是明清两代的皇家园林。"
            "御花园始建于明永乐十八年，即公元一四二零年，与故宫主体建筑同时落成。"
            "御花园东西宽约一百三十米，南北深约九十米，占地面积约一万二千平方米。"
            "园内古木参天，奇石罗布，亭台楼阁错落有致，是紫禁城中难得的自然幽静之所。"
            "园中最著名的建筑是万春亭和千秋亭，两亭东西对称，造型精美，寓意帝后万寿千秋。"
            "御花园正中是钦安殿，供奉道教真武大帝，是故宫中唯一一座道教宫殿。"
            "园内还有堆秀山，这是一座由太湖石堆砌而成的假山，山上有御景亭，可俯瞰全园。"
            "御花园的植物配置也非常讲究，以松、柏、竹、梅等传统园林植物为主。"
            "其中最引人注目的是连理柏，两株柏树树干相连，象征夫妻恩爱、百年好合。"
            "御花园虽小，却集中国古典园林艺术之大成，是皇家园林的杰出代表。"
        ),
        "en": (
            "Welcome to the Imperial Garden, located at the northern end of the central axis of the Forbidden City. "
            "It was the royal garden of the Ming and Qing dynasties. "
            "The Imperial Garden was first built in 1420, the 18th year of the Yongle reign of the Ming Dynasty, "
            "completed together with the main buildings of the Forbidden City. "
            "The garden is about 130 meters wide from east to west and 90 meters deep from north to south, "
            "covering an area of approximately 12,000 square meters. "
            "The garden features ancient trees, exotic rocks, and elegantly arranged pavilions and towers, "
            "offering a rare natural retreat within the Forbidden City. "
            "The most famous structures are the Pavilion of Myriad Springs and the Pavilion of Thousand Autumns, "
            "symmetrically placed east and west with exquisite design. "
            "At the center of the garden is the Hall of Imperial Peace, dedicated to the Daoist deity Zhenwu, "
            "the only Daoist hall in the Forbidden City. "
            "The garden also features the Hill of Accumulated Elegance, a rockery made of Taihu stones, "
            "topped by the Pavilion of Imperial View offering a panoramic view. "
            "The plantings are carefully arranged with traditional garden species like pines, cypresses, bamboo, and plum. "
            "The most striking feature is the Intertwined Cypress, two trees with connected trunks, "
            "symbolizing marital harmony and everlasting love. "
            "Though small, the Imperial Garden is a masterpiece of classical Chinese garden art."
        ),
    },
}


class ExplainPageRequest(BaseModel):
    page_id: str = Field(..., min_length=1)
    lang: str = "zh"


@app.post("/api/explain-page")
def explain_page(req: ExplainPageRequest) -> dict:
    """返回太和殿/御花园的预存讲解稿"""
    scripts = _PAGE_SCRIPTS.get(req.page_id)
    if not scripts:
        raise HTTPException(status_code=404, detail="未知页面")
    lang = req.lang if req.lang in ("zh", "en") else "zh"
    script = scripts.get(lang, scripts.get("zh", ""))
    return {"script": script, "page_id": req.page_id, "lang": lang}


class NarrationRequest(BaseModel):
    lang: str = "zh"
    page_id: str = "taihe"


@app.post("/api/narration")
def get_narration(req: NarrationRequest) -> dict:
    """返回单页AI语音导览讲解稿"""
    lang = req.lang if req.lang in ("zh", "en") else "zh"
    scripts = _PAGE_SCRIPTS.get(req.page_id)
    if not scripts:
        raise HTTPException(status_code=404, detail="未知页面")
    text = scripts.get(lang, scripts.get("zh", ""))
    return {"text": text, "lang": lang, "page_id": req.page_id}


# ── Edge TTS 语音合成（中文专用，音质好、免费）───────────────────────
class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1)
    lang: str = "zh"


_EDGE_VOICES = {
    "zh": "zh-CN-XiaoxiaoNeural",  # 晓晓 · 女声播报，温柔自然
    "en": "en-US-JennyNeural",     # Jenny · 女声
}


@app.post("/api/tts")
async def synthesize_tts(req: TTSRequest):
    """调用微软 Edge 免费在线 TTS，返回 MP3 音频流"""
    voice = _EDGE_VOICES.get(req.lang, _EDGE_VOICES["zh"])
    communicate = edge_tts.Communicate(req.text, voice)

    async def audio_stream():
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                yield chunk["data"]

    return StreamingResponse(
        audio_stream(),
        media_type="audio/mpeg",
        headers={
            "Content-Disposition": "inline",
            "X-TTS-Voice": voice,
        },
    )


@app.get("/api/files/{file_path:path}")
def get_file(file_path: str):
    target = (DOCS_DIR / file_path).resolve()
    if not str(target).startswith(str(DOCS_DIR.resolve())):
        raise HTTPException(status_code=400, detail="非法路径")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(path=target)


# ── 建筑古诗词 API ────────────────────────────────────────────────
@app.get("/api/poetry-landscape")
def api_poetry_landscape(scope: str = "all") -> dict:
    return get_poetry_landscape(scope=scope)


@app.get("/api/buildings")
def api_list_buildings(province: str | None = None) -> dict:
    return list_buildings(province=province)


@app.get("/api/buildings/{building_name}/poems")
def api_poems_by_building(
    building_name: str,
    page: int = 1,
    size: int = 20,
) -> dict:
    result = list_poems_by_building(building_name, page=page, size=size)
    if result["total"] == 0 and not get_building_entry(building_name):
        raise HTTPException(status_code=404, detail="未找到该建筑")
    return result


@app.get("/api/poems")
def api_list_poems(
    keyword: str | None = None,
    author: str | None = None,
    tag: str | None = None,
    dynasty: str | None = None,
    page: int = 1,
    size: int = 20,
) -> dict:
    return list_poems(
        keyword=keyword,
        author=author,
        tag=tag,
        dynasty=dynasty,
        page=page,
        size=size,
    )


@app.get("/api/poems/stats")
def api_poems_stats() -> dict:
    return get_stats()


@app.get("/api/poems/random")
def api_random_poem(tag: str | None = None) -> dict:
    poem = random_poem(tag=tag)
    if not poem:
        raise HTTPException(status_code=404, detail="暂无诗词")
    return poem


@app.get("/api/poems/{poem_id}")
def api_get_poem(poem_id: int) -> dict:
    poem = get_poem(poem_id)
    if not poem:
        raise HTTPException(status_code=404, detail="诗词不存在")
    return poem


static_dir = PROJECT_DIR
if static_dir.exists():
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="site")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8090, reload=True)
