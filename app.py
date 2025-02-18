from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import json
import pyodbc 
import logging
import time
from contextlib import contextmanager
from tqdm import tqdm
import pandas as pd
import decimal
from datetime import datetime, date
import os
from fastapi.responses import HTMLResponse
from functools import lru_cache
from dotenv import load_dotenv
import sys

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

# Cache configuration
CACHE_SIZE = 128
CACHE_TTL = 3600  # 1 hour in seconds

app = FastAPI(title="RAF Calculator API")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Pydantic models
class Membership(BaseModel):
    MemberID: str
    DOB: str
    Gender: str
    RAType: str
    Hospice: str
    LTIMCAID: str
    NEMCAID: str
    OREC: str

    class Config:
        extra = "forbid"

class Diagnosis(BaseModel):
    MemberID: str
    FromDOS: str
    ThruDOS: str
    DxCode: str

class ProcessDataRequest(BaseModel):
    payment_year: int
    memberships: List[Membership]
    diagnoses: List[Diagnosis]

def get_db_connection():
    """Establish a connection to the database with all specified parameters."""
    try:
        conn_str = (
            'DRIVER={ODBC Driver 17 for SQL Server};'
            'SERVER=10.10.1.4;'                     # -S parameter
            'DATABASE=RAModule2;'                   # -d parameter
            'UID=karol_bhandari;'                   # -U parameter
            'PWD=P@ssword7178!;'                   # -P parameter
            'Trusted_Connection=no;'                # Disable Windows Authentication
            'TrustServerCertificate=yes;'           # -C parameter
            'Authentication=ActiveDirectoryIntegrated;'  # -G parameter
            'Connection Timeout=30;'                # -l parameter
            'Encrypt=yes;'                         # Enable encryption
            'ApplicationIntent=ReadWrite'          # Specify application intent
        )
        
        logger.info("Attempting database connection...")
        conn = pyodbc.connect(conn_str, timeout=30)  # Set connection timeout
        logger.info("Database connection successful")
        return conn
        
    except pyodbc.Error as e:
        logger.error(f"Database connection error: {str(e)}")
        logger.error(f"Connection string used: {conn_str}")
        raise


@contextmanager
def get_db_cursor():
    """Context manager for database connections."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        yield cursor
        conn.commit()
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except:
                pass
        raise
    finally:
        if cursor:
            try:
                cursor.close()
            except:
                pass
        if conn:
            try:
                conn.close()
            except:
                pass

def create_temp_tables(cursor):
    """Create temporary tables for both membership and diagnosis data."""
    cursor.execute("""
    IF OBJECT_ID('tempdb..#TempMembership') IS NOT NULL
        DROP TABLE #TempMembership;
    IF OBJECT_ID('tempdb..#TempDiagnosis') IS NOT NULL
        DROP TABLE #TempDiagnosis;
 
    CREATE TABLE #TempMembership (
        MemberID VARCHAR(50) NOT NULL,
        BirthDate DATE NOT NULL,
        Gender VARCHAR(1) NULL,
        RAType VARCHAR(10) NULL,
        Hospice VARCHAR(1) NULL,
        LTIMCAID VARCHAR(1) NULL,
        NEMCAID VARCHAR(1) NULL,
        OREC VARCHAR(1) NULL
    );
 
    CREATE TABLE #TempDiagnosis (
        MemberID VARCHAR(50) NOT NULL,
        FromDOS DATE NOT NULL,
        ThruDOS DATE NOT NULL,
        DxCode VARCHAR(20) NOT NULL
    );
    """)

@lru_cache(maxsize=CACHE_SIZE)
def process_data_with_sp_cached(payment_year: int, memberships_tuple: tuple, diagnoses_tuple: tuple):
    """Cached version of the data processing function."""
    try:
        memberships = [dict(m) for m in memberships_tuple]
        diagnoses = [dict(d) for d in diagnoses_tuple]
        with get_db_cursor() as cursor:
            return process_data_with_sp(cursor, payment_year, memberships, diagnoses)
    except Exception as e:
        logger.error(f"Cache processing error: {str(e)}")
        raise

def process_data_with_sp(cursor, payment_year, memberships, diagnoses):
    """Process data using the stored procedure."""
    try:
        create_temp_tables(cursor)
        logger.info('Temp tables created successfully')
 
        df_members = pd.DataFrame(memberships)
        df_members = df_members.rename(columns={'DOB': 'BirthDate'})
        total_members = len(df_members)
        batch_size = 1000
 
        logger.info("Inserting membership data...")
        for i in tqdm(range(0, total_members, batch_size), desc="Processing members"):
            batch = df_members.iloc[i:i+batch_size]
            values = ','.join([
                f"('{row.MemberID}', '{row.BirthDate}', '{row.Gender}', '{row.RAType}', "
                f"'{row.Hospice}', '{row.get('LTIMCAID', 'N')}', '{row.get('NEMCAID', 'N')}', "
                f"'{row.OREC}')"
                for _, row in batch.iterrows()
            ])
            if values:
                cursor.execute(f"INSERT INTO #TempMembership VALUES {values}")
 
        df_diag = pd.DataFrame(diagnoses)
        total_diag = len(df_diag)
 
        logger.info("Inserting diagnosis data...")
        for i in tqdm(range(0, total_diag, batch_size), desc="Processing diagnoses"):
            batch = df_diag.iloc[i:i+batch_size]
            values = ','.join([
                f"('{row.MemberID}', '{row.FromDOS}', '{row.ThruDOS}', '{row.DxCode}')"
                for _, row in batch.iterrows()
            ])
            if values:
                cursor.execute(f"INSERT INTO #TempDiagnosis VALUES {values}")
 
        logger.info('Executing stored procedure...')
        sql = """
            DECLARE @PmtYear INT = ?;
            DECLARE @Membership AS InputMembership_PartC;
            DECLARE @DxTable AS InputDiagnosis;
            INSERT INTO @Membership (
                MemberID, BirthDate, Gender, RAType, 
                Hospice, LTIMCAID, NEMCAID, OREC
            )
            SELECT 
                MemberID, BirthDate, Gender, RAType,
                Hospice, LTIMCAID, NEMCAID, OREC
            FROM #TempMembership;
            INSERT INTO @DxTable (MemberID, FromDOS, ThruDOS, DxCode)
            SELECT MemberID, FromDOS, ThruDOS, DxCode
            FROM #TempDiagnosis;
 
            EXEC dbo.sp_RS_Medicare_PartC_Outer @PmtYear, @Membership, @DxTable;
        """
        cursor.execute(sql, payment_year)
        
        while cursor.description is None and cursor.nextset():
            pass
        if cursor.description is None:
            return []
        
        columns = [column[0] for column in cursor.description]
        results = []
        for row in cursor.fetchall():
            row_dict = {}
            for i, value in enumerate(row):
                column_name = columns[i]
                if isinstance(value, decimal.Decimal):
                    row_dict[column_name] = float(value)
                elif isinstance(value, (datetime, date)):
                    row_dict[column_name] = value.isoformat()
                elif value is None:
                    row_dict[column_name] = None
                else:
                    row_dict[column_name] = str(value)
            results.append(row_dict)
        
        logger.info(f"Retrieved {len(results)} records from stored procedure")
        return results
 
    except Exception as e:
        logger.error(f"Error in process_data_with_sp: {str(e)}")
        raise

@app.get("/")
async def root():
    return {"message": "Welcome to RAF Calculator API"}

@app.post("/process_data")
async def process_data(request: ProcessDataRequest):
    """API endpoint to handle data processing with caching."""
    try:
        logger.info(f"Processing data for {len(request.memberships)} members and {len(request.diagnoses)} diagnoses")
        memberships_dict = [membership.dict() for membership in request.memberships]
        diagnoses_dict = [diagnosis.dict() for diagnosis in request.diagnoses]
        memberships_tuple = tuple(tuple(sorted(m.items())) for m in memberships_dict)
        diagnoses_tuple = tuple(tuple(sorted(d.items())) for d in diagnoses_dict)
        
        try:
            results = process_data_with_sp_cached(
                request.payment_year,
                memberships_tuple,
                diagnoses_tuple
            )
            cache_status = "Cache hit"
        except Exception as e:
            logger.error(f"Cache error: {str(e)}")
            process_data_with_sp_cached.cache_clear()
            results = process_data_with_sp_cached(
                request.payment_year,
                memberships_tuple,
                diagnoses_tuple
            )
            cache_status = "Cache miss"
            
        response_data = {
            'status': 'success',
            'message': 'Data processed successfully',
            'cache_status': cache_status,
            'count': len(results),
            'results': results,
            'timestamp': datetime.now().isoformat()
        }
        logger.info(f"Successfully processed {len(results)} records ({cache_status})")
        return response_data
 
    except Exception as e:
        error_message = str(e)
        logger.error(f"Error: {error_message}")
        raise HTTPException(
            status_code=500,
            detail={
                'status': 'error',
                'message': 'Internal server error',
                'error': error_message,
                'timestamp': datetime.now().isoformat()
            }
        )
@app.get("/health")
async def health_check():
    return {"status": "healthy"}
if __name__ == '__main__':
    import uvicorn
    port = int(os.getenv("PORT", 443))
    uvicorn.run(app, host='0.0.0.0', port=port)
