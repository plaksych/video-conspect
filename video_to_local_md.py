import os
import re
import json
import yt_dlp
from openai import OpenAI

# Настройки локального сервера llama.cpp
LLAMA_SERVER_URL = "http://localhost:8080/v1"
OUTPUT_DIR = "./lessons_summaries"


def get_video_id(url):
    """Извлекает ID видео из ссылки YouTube и отсекает параметры плейлистов"""
    match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', url)
    return match.group(1) if match else None


def get_transcript_via_ytdlp(video_url):
    ydl_opts = {
        'writesubtitles': True,
        'writeautomaticsubs': True,
        'subtitleslangs': ['ru', 'en'],
        'skip_download': True,
        
        # --- ФИНАЛЬНОЕ ИСПРАВЛЕНИЕ ОШИБКИ ФОРМАТОВ ---
        # Инструктируем yt-dlp игнорировать видеопотоки и смотреть только на аудио/базовые контейнеры
        'format': 'ba/b', 
        'ignoreerrors': True, # Не падать, если какой-то второстепенный поток недоступен
        
        # --- ПРОКСИ ДЛЯ ОБХОДА БЛОКИРОВКИ ---
        'proxy': 'socks5://127.0.0.1:10808',
        # --- ДОБАВЛЯЕМ КУКИ ДЛЯ ОБХОДА ОШИБКИ 429 ---
        'cookiefile': 'www.youtube.com_cookies.txt',
        
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 10,
        'retries': 2,
        'extractor_args': {
            'youtube': ['player_client=android,web']
        }
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # extract_info с process=False извлекает только метаданные (включая ссылки на субтитры)
            # без попыток симулировать выбор видео-формата для загрузки
            info = ydl.extract_info(video_url, download=False, process=False)
            
            # Если из-за process=False данные упакованы в список entries, берем первый элемент
            if 'entries' in info:
                info = info['entries'][0]
                
            # Повторно запрашиваем точные субтитры, если они сбросились
            subtitles = info.get('subtitles', {})
            auto_subtitles = info.get('automatic_captions', {})
            
            # Если на упрощенном этапе метаданные субтитров пустые, делаем один полный быстрый проход
            if not subtitles and not auto_subtitles:
                ydl_opts['format'] = 'worst' # Переключаем на самый легкий формат из существующих
                with yt_dlp.YoutubeDL(ydl_opts) as ydl_retry:
                    info = ydl_retry.extract_info(video_url, download=False)
                    subtitles = info.get('subtitles', {})
                    auto_subtitles = info.get('automatic_captions', {})

            lang = None
            subs_data = None
            
            for l in ['ru', 'en']:
                if l in subtitles:
                    lang = l
                    subs_data = subtitles[l]
                    break
                elif l in auto_subtitles:
                    lang = l
                    subs_data = auto_subtitles[l]
                    break
            
            if not subs_data:
                print("[ОШИБКА] На видео не обнаружено ни русских, ни английских субтитров.")
                return None
            
            json3_url = None
            for ext in ['json3', 'srv1', 'vtt']:
                json3_url = next((sub['url'] for sub in subs_data if sub.get('ext') == ext), None)
                if json3_url:
                    break
                    
            if not json3_url:
                json3_url = subs_data[0]['url']
            
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
                full_text = re.sub(r'\d{2}:\d{2}:\d{2}\.\d.7.*?\n', '', content)
                full_text = re.sub(r'<.*?>', '', full_text)
                full_text = "\n".join([line.strip() for line in full_text.splitlines() if line.strip() and not line.strip().isdigit()])

            return full_text.strip()
            
    except Exception as e:
        print(f"\n[ОШИБКА] Не удалось извлечь субтитры: {e}")
        return None


def generate_md_summary(text):
    """Отправляет текст в локальную модель llama.cpp для создания конспекта"""
    client = OpenAI(
        base_url=LLAMA_SERVER_URL,
        api_key="not-needed-for-local"
    )
    
    prompt = (
    "Ты — ассистент, который пишет подробные конспекты лекций и учебных видео.\n\n"
    "Твоя задача: на основе расшифровки ниже написать **развёрнутый структурированный конспект** на русском языке.\n\n"
    "Требования:\n"
    "- Сохраняй все важные детали, объяснения, примеры и аргументы — не сворачивай их в одну фразу\n"
    "- Если автор что-то объясняет пошагово или приводит пример — включи это в конспект\n"
    "- Используй Markdown: заголовки (##, ###), маркированные и нумерованные списки, таблицы где уместно\n"
    "- Группируй материал по смысловым блокам с заголовками\n"
    "- Убирай только воду: приветствия, повторы, слова-паразиты — но не содержательные пояснения\n"
    "- Объём конспекта должен отражать объём и насыщенность исходного материала\n\n"
    f"Расшифровка:\n{text}"
)
    
    try:
        response = client.chat.completions.create(
            model="local-model",
            messages=[{"role": "user", "content": prompt}],
            # Оптимальные параметры для удержания структуры без зацикливания
            temperature=0.3,
            top_p=0.9,
            frequency_penalty=0.4,
            presence_penalty=0.2,
            max_tokens=4096
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"Ошибка при работе с моделью: {e}")
        print("Проверьте, запущен ли сервер llama.cpp (по умолчанию http://localhost:8080).")
        return None


def main():
    video_url_raw = input("Введите ссылку на YouTube видео: ").strip()
    video_id = get_video_id(video_url_raw)
    
    if not video_id:
        print("[ОШИБКА] Не удалось распознать URL видео.")
        return
    
    # Формируем чистую ссылку для yt-dlp
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    
    print("\n1. Получение субтитров из YouTube...")
    raw_text = get_transcript_via_ytdlp(video_url)
    if not raw_text: 
        return
    
    print("2. Генерация конспекта локальной нейросетью...")
    md_content = generate_md_summary(raw_text)
    if not md_content:
        return
        
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
        
    file_name = f"summary_{video_id}.md"
    file_path = os.path.join(OUTPUT_DIR, file_name)
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(md_content)
        
    print(f"\n[УСПЕХ] Конспект успешно сохранен локально!")
    print(f"Путь к файлу: {os.path.abspath(file_path)}")


if __name__ == "__main__":
    main()