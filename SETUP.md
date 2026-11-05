# 🚀 DateFlow Pipeline — Полная инструкция по постоянной работе

## Где мы сейчас
✅ Контент-пайплайн работает: 24 поста уже опубликовано в @dataflow_planner
✅ Reels рендерятся автоматически через MoneyPrinterTurbo (качественно, word-by-word субтитры)
✅ Buffer публикует в инстаграм в заданное время
❌ Последняя проблема: хостинг видео (нужно обновить pipeline.py по инструкции ниже)

## 🎯 Цель
4 Reels в день автоматически без вашего участия. Рост до 100K подписчиков органически.

---

## Шаг 1: Обновить pipeline.py (РЕШАЕТ ПРОБЛЕМУ С ЗАГРУЗКОЙ)

Это самый важный шаг — исправляет ошибку "All uploaders failed".

1. Откройте репозиторий: https://github.com/darlahunter1-netizen/dateflow-pipeline
2. Нажмите на файл `pipeline.py`
3. Справа сверху иконка ✏️ **Edit this file** (карандаш)
4. Нажмите **Ctrl+A** чтобы выделить весь текст → **Delete**
5. Откройте файл `pipeline.py` из этого архива (внутри zip), скопируйте всё и вставьте в редактор на GitHub
6. Внизу страницы нажмите зелёную кнопку **Commit changes** → ещё раз **Commit changes**

## Шаг 2: Обновить workflow файл

1. В репозитории откройте папку `.github` → `workflows` → `daily-reel.yml`
2. ✏️ Edit → Ctrl+A → Delete → вставьте текст:

```yaml
name: DateFlow Daily Reels (MPT-powered, 4 posts/day)
on:
  schedule:
    - cron: '0 7 * * *'
  workflow_dispatch:

permissions:
  contents: write

jobs:
  build-and-publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install system deps
        run: |
          sudo apt-get update -q && sudo apt-get install -y -q ffmpeg fonts-dejavu curl git
          curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
          sudo apt-get install -y nodejs
          sudo npm install -g @bufferapp/cli

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Clone & install MoneyPrinterTurbo
        run: |
          cd $GITHUB_WORKSPACE/..
          git clone --depth 1 https://github.com/harry0703/MoneyPrinterTurbo.git mpt
          cd mpt
          pip install -r requirements.txt --quiet
          mkdir -p resource/fonts
          curl -sL -o resource/fonts/Anton-Regular.ttf "https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf"

      - name: Install pipeline deps
        run: pip install requests openai --quiet

      - name: Setup Buffer CLI config
        env:
          BUFFER_API_KEY: ${{ secrets.BUFFER_API_KEY }}
          BUFFER_ORG_ID: ${{ secrets.BUFFER_ORG_ID }}
        run: bash setup_buffer.sh

      - name: Generate & publish 4 Reels
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          PEXELS_API_KEY: ${{ secrets.PEXELS_API_KEY }}
          BUFFER_API_KEY: ${{ secrets.BUFFER_API_KEY }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          ADMIN_CHAT_ID: ${{ secrets.ADMIN_CHAT_ID }}
          BUFFER_CHANNEL_ID: ${{ secrets.BUFFER_CHANNEL_ID }}
          BUFFER_ORG_ID: ${{ secrets.BUFFER_ORG_ID }}
          MPT_DIR: ${{ github.workspace }}/../mpt
          NUM_POSTS: ${{ vars.NUM_POSTS || '4' }}
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          GITHUB_REPOSITORY: ${{ github.repository }}
        run: python pipeline.py

      - name: Upload Reel artifacts (debug)
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: daily-reels
          path: output/
          retention-days: 14
```

3. **Commit changes** → **Commit changes**

## Шаг 3: Проверить права Actions
1. **Settings** (вверху справа)
2. В левом меню: **Actions** → **General**
3. Пролистайте до раздела **Workflow permissions**
4. Выберите ✅ **Read and write permissions**
5. Нажмите **Save**

## Шаг 4: Проверить секреты
Settings → Secrets and variables → Actions → Secrets. Должны быть ровно эти 7 секретов (проверьте названия — большими буквами, с подчёркиваниями):

| Name | Value |
|---|---|
| `OPENAI_API_KEY` | `sk-3e4cb9080fea4d2b9c872aea51cc2ec4` |
| `PEXELS_API_KEY` | `eaYdwAVy9MeKRY0okZClhbND6hkwAyXHcds0aXXn4ITCamkdGc4nkpdb` |
| `BUFFER_API_KEY` | `HTR09W8Ubn_r5n6a9wDaYDPFCysHlqEIdYOORVzvtKJ` |
| `BUFFER_ORG_ID` | `6ad678fa27469e130f893e13` |
| `BUFFER_CHANNEL_ID` | `6adc7b9b778b240e2fac6af1` |
| `TELEGRAM_BOT_TOKEN` | `8795932131:AAGvoGY6DuROOSe4hxE_71rofDOvu4iDJJ0` |
| `ADMIN_CHAT_ID` | `7210384965` |

Если какого-то нет — **New repository secret** и добавьте. Если опечатка в названии — удалите и создайте заново.

## Шаг 5: Отменить зависшие запуски
1. Вкладка **Actions**
2. Если есть запуск со статусом Queued (жёлтый кружок) и давностью больше часа — нажмите на него → кнопка **Cancel workflow** справа вверху

## Шаг 6: Очистить Buffer
Откройте https://publish.buffer.com
- Если в Queue или Approvals есть посты с ошибкой ❌ "Please update the media URL" — удалите их через ⋮ → Delete (это старые посты с мёртвыми ссылками, они всё равно не опубликуются)
- Очередь должна быть пустой или только с новыми корректными постами

## Шаг 7: Тестовый запуск
1. Вкладка **Actions** → слева нажмите "DateFlow Daily Reels (MPT-powered, 4 posts/day)"
2. Справа кнопка **Run workflow** → выпадающее меню → ещё раз **Run workflow**
3. Нажмите на появившийся жёлтый кружок → **build-and-publish** чтобы смотреть логи
4. Подождите 20-30 минут. В логе будет видно:
   - `🌹 DateFlow MPT Auto-Pilot starting — 4 reel(s)`
   - Каждый Reel: `🎬 Reel 1/4 → ...` → `✅ MPT render complete` → `✅ Upload (uguu): https://...` → `✅ Reel #1 scheduled for ...`
   - В конце: `🎉 Batch complete: 4/4 reels scheduled`

5. Проверьте Telegram бота @DateFlow_Planner_bot — должны прийти 4 видео с подтверждениями
6. Проверьте Buffer https://publish.buffer.com — в очереди должно быть 4 поста на 11:00, 15:00, 18:15, 21:30

---

## 📅 Как это работает дальше (постоянно)

- **Каждый день в 10:00 TRT (07:00 UTC)** GitHub Actions запускается автоматически по расписанию (`cron: '0 7 * * *'`)
- Пайплайн смотрит какие слоты уже заняты в Buffer и создаёт НОВЫЕ видео только на свободные слоты (не дублирует)
- 4 Reels автоматически генерируются и ставятся в очередь Buffer
- Buffer сам публикует в Instagram в 11:00 / 15:00 / 18:15 / 21:30
- В Telegram приходит сводка с 4 видео и статусом

## 📊 Проверка один раз в день

Ничего делать не нужно, но если хотите контролировать:
1. **В 10:10-10:30 утра** загляните в Telegram — должна прийти сводка "🎉 Batch complete: 4/4 reels scheduled"
2. Если там "0/4" или ошибки — откройте лог Actions и посмотрите на какой step упало. Обычно само исправляется на следующий день.
3. В Buffer https://publish.buffer.com в Queue должно быть 4 поста на сегодня (утром)
4. Вечером можно посмотреть статистику в Instagram Insights

## ⚠️ Лимиты и что может сломаться

| Проблема | Решение |
|---|---|
| Закончились посты в Buffer (оранжевый Approvals) | Бесплатный план — 10 постов в неделю. После 50/week подождите неделю или купите платный Buffer |
| API ключ DeepSeek перестал работать | Получите новый ключ на platform.deepseek.com или смените на OpenAI/другую модель в `pipeline.py` |
| Actions не запустился по расписанию | Бывает у GitHub бесплатных планов (редко). Просто нажмите Run workflow вручную один раз |
| Pexels не отдал видео | Pexels иногда банит IP, автоматически используется fallback — не обращайте внимания |
| Все хосты для видео упали | Напишите мне, добавлю новый хост |
| Закончилось место на GitHub Release | На бесплатном плане GitHub Releases не имеют лимита на количество ассетов, но периодически старые можно удалять |

## 🔑 Важные ссылки (сохраните)

- Репозиторий: https://github.com/darlahunter1-netizen/dateflow-pipeline
- Actions (запуски): https://github.com/darlahunter1-netizen/dateflow-pipeline/actions
- Buffer (очередь): https://publish.buffer.com
- Instagram: https://instagram.com/dataflow_planner
- DateFlow бот: t.me/DateFlow_Planner_bot
- DateFlow приложение: https://dateflow-tau.vercel.app/

## 📈 План по росту (ближайшие недели)

1. **Неделя 1-2**: 4 поста в день только на английском, стабильный выход по расписанию → 200-1000 подписчиков
2. **Неделя 3**: Добавим турецкие Reels (половина турецких, половина английских) — турецкая аудитория в Стамбуле взрывает охват
3. **Неделя 4**: Определим вирусные темы (какие Reels набрали больше просмотров) и будем делать больше такого контента
4. **Далее**: Добавим трендовые BGM, пин-комменты, Stories-опросы, CTA на подписку в каждом видео

Если видите что в какой-то день Reels не вышли или в Telegram ошибка — скиньте скриншот лога, я поправлю. В остальном — всё должно работать на автопилоте 🚀
