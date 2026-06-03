import os
import re
import json
import yt_dlp
from openai import OpenAI

# Настройки сервера
LLAMA_SERVER_URL = "http://localhost:8080/v1"
OUTPUT_DIR = "./lessons_summaries"


def get_video_id(url):
    match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', url)
    return match.group(1) if match else None


def get_transcript_via_ytdlp(video_url):
    print("   (Запускаю yt-dlp для извлечения субтитров...)")
    ydl_opts = {
        'writesubtitles': True,
        'writeautomaticsubs': True,
        'subtitleslangs': ['ru', 'en'],
        'skip_download': True,
        'proxy': 'socks5://127.0.0.1:10808', # Замените на адрес и порт вашего прокси/VPN клиента
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 10,
        'retries': 2,
        'extractor_args': {'youtube': ['player_client=android,web']}
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            subtitles = info.get('subtitles', {})
            auto_subtitles = info.get('automatic_captions', {})
            
            lang, subs_data = None, None
            for l in ['ru', 'en']:
                if l in subtitles: lang, subs_data = l, subtitles[l]; break
                elif l in auto_subtitles: lang, subs_data = l, auto_subtitles[l]; break
            
            if not subs_data: return None
            
            json3_url = next((sub['url'] for sub in subs_data if sub.get('ext') == 'json3'), None)
            if not json3_url: json3_url = subs_data[0]['url']
            
            with ydl.urlopen(json3_url) as response:
                content = response.read().decode('utf-8')
            
            if '"events"' in content:
                data = json.loads(content)
                text_segments = []
                for event in data.get('events', []):
                    if 'segs' in event:
                        for seg in event['segs']:
                            text_segments.append(seg['utf8'])
                full_text = "".join(text_segments)
            else:
                full_text = re.sub(r'\d{2}:\d{2}:\d{2}\.\d{3}.*?\n', '', content)
                full_text = re.sub(r'<.*?>', '', full_text)
                full_text = "\n".join([line.strip() for line in full_text.splitlines() if line.strip() and not line.strip().isdigit()])

            print(f"   [ОК] Субтитры загружены (Язык: {lang})")
            return full_text.strip()
    except Exception as e:
        print(f"[ОШИБКА] Не удалось получить субтитры: {e}")
        return None


def chunk_text(text, max_words=1500):
    """Разбивает текст на куски по словам, чтобы модель не теряла фокус"""
    words = text.split()
    chunks = []
    current_chunk = []
    current_count = 0
    
    for word in words:
        current_chunk.append(word)
        current_count += 1
        if current_count >= max_words:
            chunks.append(" ".join(current_chunk))
            current_chunk = []
            current_count = 0
            
    if current_chunk:
        chunks.append(" ".join(current_chunk))
    return chunks


def process_chunk(client, chunk_text, chunk_index, total_chunks):
    """Этап 1: Сжимаем отдельный кусок текста"""
    print(f"   -> Анализ части {chunk_index}/{total_chunks}...")
    
    system_instruction = (
        "Ты — аналитик. Твоя задача — выделить все ключевые тезисы, факты, термины и важные мысли "
        "из предоставленного фрагмента лекции. Пиши тезисно, строго по делу, без вводных слов."
    )
    
    try:
        response = client.chat.completions.create(
            model="local-model",
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": f"Фрагмент текста:\n{chunk_text}"}
            ],
            temperature=0.1,
            top_p=0.9
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"Ошибка куска {chunk_index}: {e}")
        return ""


def generate_final_markdown(client, merged_notes):
    """Этап 2: Превращаем все выжимки в один красивый структурированный Markdown документ"""
    print("   -> Сборка финального Markdown документа...")
    
    system_instruction = (
        "Ты — профессиональный технический писатель. Перед тобой черновые тезисы из видеолекции. "
        "Собери из них один структурированный, логичный конспект в формате Markdown.\n"
        "Правила оформления:\n"
        "1. Используй четкую иерархию заголовков: Главная тема (#), подтемы (##), детали (###).\n"
        "2. Важные термины выделяй **жирным шрифтом**.\n"
        "3. Списки делай через дефис (-).\n"
        "4. Если есть сравнения, сущности или технические параметры — оформи их в виде Markdown-таблицы.\n"
        "5. Убери любые повторы мыслей. Конспект должен быть чистым, глубоким и профессиональным."
    )
    
    try:
        response = client.chat.completions.create(
            model="local-model",
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": f"Черновые тезисы:\n{merged_notes}"}
            ],
            temperature=0.2,
            top_p=0.9,
            max_tokens=4096
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"Ошибка финальной сборки: {e}")
        return None


def main():
    video_url = input("Введите ссылку на YouTube видео: ").strip()
    video_id = get_video_id(video_url)
    if not video_id: return

    raw_text = get_transcript_via_ytdlp(video_url)
    if not raw_text: return

    client = OpenAI(base_url=LLAMA_SERVER_URL, api_key="not-needed")

    print("2. Обработка текста нейросетью...")
    # Нарезаем текст на порции примерно по 1500 слов (модели будет легко с ними работать)
    chunks = chunk_text(raw_text, max_words=1500)
    
    intermediate_summaries = []
    for i, chunk in enumerate(chunks, 1):
        summary = process_chunk(client, chunk, i, len(chunks))
        if summary:
            intermediate_summaries.append(summary)

    # Соединяем промежуточные выжимки
    all_notes = "\n\n=== СЛЕДУЮЩИЙ БЛОК ТЕЗИСОВ ===\n\n".join(intermediate_summaries)

    # Делаем из этого финальную конфету
    final_md = generate_final_markdown(client, all_notes)
    if not final_md: return

    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)

    file_path = os.path.join(OUTPUT_DIR, f"summary_{video_id}.md")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(final_md)

    print(f"\n[УСПЕХ] Структурированный конспект сохранен!")
    print(f"Путь: {os.path.abspath(file_path)}")


if __name__ == "__main__":
    main()