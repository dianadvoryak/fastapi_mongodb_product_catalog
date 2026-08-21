from fastapi import FastAPI, HTTPException, Query 
from pydantic import BaseModel, Field
from motor.motor_asyncio import AsyncIOMotorClient
import pymongo
from typing import List, Dict, Any
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional 
from bson import ObjectId


app = FastAPI(title="Python MongoDB API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Разрешает запросы с любых адресов (включая ваш Swagger)
    allow_credentials=True,
    allow_methods=["*"], # Разрешает любые методы (GET, POST, PUT, DELETE)
    allow_headers=["*"], # Разрешает любые заголовки
)


# ВАЖНО: Драйвер должен знать точные внешние порты нод из docker-compose
# mongo1 -> 27017, mongo2 -> 27018, mongo3 -> 27019
MONGO_URI = "mongodb://mongo1:27017,mongo2:27018,mongo3:27019/?replicaSet=rs0&serverSelectionTimeoutMS=5000"
# Подключаемся к первой ноде по IP, и говорим драйверу не опрашивать имена остальных нод
MONGO_URI = "mongodb://127.0.0.1:27017/?directConnection=true&serverSelectionTimeoutMS=5000"

client = AsyncIOMotorClient(MONGO_URI)
db = client.pet_project

# СХЕМА ДАННЫХ ДЛЯ SWAGGER (Pydantic)
class ProductModel(BaseModel):
    store_id: int = Field(..., description="ID магазина для шардинга", example=101)
    sku: str = Field(..., description="Артикул товара", example="SM-G998B")
    name: str = Field(..., description="Название товара", example="Samsung S21")
    slug: str = Field(..., description="Человекочитаемый URL", example="samsung-s21")
    description: str = Field(..., description="Описание", example="Флагманский телефон")
    price: float = Field(..., description="Цена", example=899.99)
    attributes: List[Dict[str, Any]] = Field(default=[], description="Динамические NoSQL свойства")


class UpdateProductModel(BaseModel):
    store_id: Optional[int] = Field(None, description="ID магазина", example=101)
    sku: Optional[str] = Field(None, description="Артикул товара", example="SM-G998B")
    name: Optional[str] = Field(None, description="Название товара", example="Samsung S21")
    slug: Optional[str] = Field(None, description="Человекочитаемый URL", example="samsung-s21")
    description: Optional[str] = Field(None, description="Описание", example="Обновленное описание")
    price: Optional[float] = Field(None, description="Цена", example=799.99)
    attributes: Optional[List[Dict[str, Any]]] = Field(None, description="Динамические свойства")


# АВТО-МИГРАЦИЯ (Создание индексов при старте приложения)
@app.on_event("startup")
async def create_db_indexes():
    print("🚀 Проверка и создание индексов в MongoDB...")
    try:
        # 1. Составной индекс (store_id + sku) для Шардинга
        await db.products.create_index(
            [("store_id", pymongo.ASCENDING), ("sku", pymongo.ASCENDING)],
            name="products_store_sku_idx"
        )
        # 2. Уникальный индекс для slug
        await db.products.create_index(
            [("slug", pymongo.ASCENDING)],
            unique=True,
            name="products_slug_unique_idx"
        )
        # 3. Вложенный индекс для динамических характеристик товара
        await db.products.create_index(
            [("attributes.key", pymongo.ASCENDING), ("attributes.value", pymongo.ASCENDING)], # Сначала данные сортируются по первому полю (attributes.key от А до Я). А если ключи одинаковые, они сортируются по второму полю (attributes.value от А до Я).
            name="products_attributes_idx"
        )
        # 4. Полнотекстовый индекс для поиска по названию и описанию
        await db.products.create_index(
            [("name", pymongo.TEXT), ("description", pymongo.TEXT)],
            name="products_fulltext_idx"
        )
        print("✅ Все индексы успешно проверены и применены!")
    except Exception as e:
        print(f"❌ Ошибка подключения или создания индексов: {e}")

# ЭНДПОИНТ: Создание товара
@app.post("/api/v1/products", response_model=ProductModel, status_code=201)
async def create_product(product: ProductModel):
    product_dict = product.model_dump()
    try:
        result = await db.products.insert_one(product_dict)
        if result.inserted_id:
            return product_dict
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка сохранения: {e}")
    raise HTTPException(status_code=400, detail="Не удалось сохранить товар")

# ЭНДПОИНТ: Получить все товары из базы данных
@app.get("/api/v1/products", response_model=List[ProductModel])
async def get_products():
    products_list = []
    # Запрашиваем все документы из коллекции products
    cursor = db.products.find() 
    
    async for document in cursor:
        # Убираем внутренний MongoDB ID (_id), так как Pydantic модель его не ждет
        document.pop("_id", None) 
        products_list.append(document)
        
    return products_list


# ЭНДПОИНТ: Полнотекстовый поиск товаров
@app.get("/api/v1/products/search", response_model=List[Dict[str, Any]])
async def search_products(q: str = Query(..., description="Поисковый запрос (например: Samsung телефон)")):
    try:
        products_list = []
        
        # Делаем поисковый запрос к MongoDB
        cursor = db.products.find(
            # 1. Ищем по текстовому индексу
            {"$text": {"$search": q}},
            # 2. Просим Mongo посчитать текстовый вес (релевантность) для каждого совпадения
            {"score": {"$meta": "textScore"}}
        ).sort(
            # 3. Сортируем результаты: самые подходящие товары будут первыми
            [("score", {"$meta": "textScore"})]
        )
        
        async for document in cursor:
            # Превращаем ObjectId в строку, чтобы JSON не ломался при выдаче
            document["_id"] = str(document["_id"])
            products_list.append(document)
            
        return products_list

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка при поиске: {e}")


# 1. ЭНДПОИНТ: Получить один товар по его ObjectId
@app.get("/api/v1/products/{id}", response_model=Dict[str, Any])
async def get_product_by_id(id: str):
    try:
        # Проверяем, является ли переданная строка валидным ObjectId
        if not ObjectId.is_valid(id):
            raise HTTPException(status_code=400, detail="Неверный формат MongoDB ObjectId")
        
        # Ищем документ в базе данных, оборачивая строковый id в ObjectId()
        product = await db.products.find_one({"_id": ObjectId(id)})
        
        if product:
            product["_id"] = str(product["_id"]) # Конвертируем обратно в строку для JSON
            return product
            
        raise HTTPException(status_code=404, detail="Товар не найден")
    except Exception as e:
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=f"Ошибка сервера: {e}")


# 2. ЭНДПОИНТ: Обновить товар (Частичное обновление)
@app.put("/api/v1/products/{id}", response_model=Dict[str, Any])
async def update_product(id: str, product_data: UpdateProductModel):
    try:
        if not ObjectId.is_valid(id):
            raise HTTPException(status_code=400, detail="Неверный формат MongoDB ObjectId")
            
        # Превращаем модель Pydantic в словарь и удаляем поля, которые пользователь не прислал (None)
        update_data = {k: v for k, v in product_data.model_dump().items() if v is not None}
        
        if not update_data:
            raise HTTPException(status_code=400, detail="Не передано ни одного поля для обновления")
            
        # Используем оператор MongoDB "$set" для частичного обновления документа
        result = await db.products.update_one(
            {"_id": ObjectId(id)},
            {"$set": update_data}
        )
        
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Товар для обновления не найден")
            
        # Возвращаем обновленный товар из базы
        updated_product = await db.products.find_one({"_id": ObjectId(id)})
        updated_product["_id"] = str(updated_product["_id"])
        return updated_product

    except Exception as e:
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=f"Ошибка сервера: {e}")

# 3. ЭНДПОИНТ: Удалить товар по его ObjectId
@app.delete("/api/v1/products/{id}", status_code=200)
async def delete_product(id: str):
    try:
        # Проверяем, является ли переданная строка валидным ObjectId
        if not ObjectId.is_valid(id):
            raise HTTPException(status_code=400, detail="Неверный формат MongoDB ObjectId")
            
        # Выполняем удаление одного документа в коллекции products
        result = await db.products.delete_one({"_id": ObjectId(id)})
        
        # Если удалено 0 документов, значит товара с таким ID не существовало
        if result.deleted_count == 0:
            raise HTTPException(status_code=404, detail="Товар для удаления не найден")
            
        # Возвращаем успешный статус и подтверждение
        return {"status": "success", "message": f"Товар с ID {id} успешно удален"}

    except Exception as e:
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=f"Ошибка сервера при удалении: {e}")
