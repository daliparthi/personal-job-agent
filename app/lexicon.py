"""
ATS keyword dictionary. One line per skill: Canonical|alias|alias...
Matching is case-insensitive on whole words. Add your own lines freely.
Lines in SOFT_SKILLS count half as much toward the match score.
"""

HARD_SKILLS = r"""
Python|Python3
Java|Java 8|Java 11|Java 17
JavaScript|ECMAScript|ES6
TypeScript
C++|CPP
C#|C Sharp
Golang|Go language|Go programming
Rust
Scala
Kotlin
Swift
Objective-C
Ruby
PHP
Perl
R programming|R language|RStudio|R Shiny
MATLAB
SQL
PL/SQL
T-SQL|TSQL
NoSQL
Bash|Shell scripting|Shell script|Unix shell
PowerShell
VBA
COBOL
Verilog
SystemVerilog
VHDL
CUDA
OpenCL
Assembly language|x86 assembly|ARM assembly
Dart
Elixir
Haskell
Julia
React|React.js|ReactJS
Angular|AngularJS
Vue|Vue.js|VueJS
Next.js|NextJS
Node.js|NodeJS
Express.js|ExpressJS
Django
Flask
FastAPI
Spring Boot|Spring Framework|Spring
Hibernate
.NET|ASP.NET|.NET Core|dotnet
Ruby on Rails|Rails
Laravel
GraphQL
REST APIs|REST API|RESTful|REST
gRPC
HTML|HTML5
CSS|CSS3|SASS|SCSS
Tailwind
jQuery
Redux
Webpack
Microservices|Microservice
SOAP
OAuth|OAuth2|OAuth 2.0
WebSockets|WebSocket
iOS
Android
React Native
Flutter
SwiftUI
AWS|Amazon Web Services
Azure|Microsoft Azure
GCP|Google Cloud|Google Cloud Platform
EC2
S3|Amazon S3
AWS Lambda
EKS
ECS
CloudFormation
DynamoDB
Redshift
AWS Glue
Athena
EMR
Kinesis
SageMaker
Bedrock
Azure Data Factory|ADF
Azure DevOps
Azure Functions
Azure Synapse|Synapse
BigQuery
Dataflow
Pub/Sub
Cloud Run
OpenShift
Serverless
Multi-cloud|Multicloud|Hybrid cloud
SaaS
Docker|containers|containerization
Kubernetes|K8s
Terraform
Ansible
Puppet
Chef
Jenkins
GitHub Actions
GitLab|GitLab CI
CircleCI
Argo CD|ArgoCD|Argo
Helm
CI/CD|CICD|continuous integration|continuous delivery|continuous deployment
Git
GitHub
Bitbucket
Linux|RHEL|Ubuntu|CentOS
Unix
Windows Server
Prometheus
Grafana
Datadog
Splunk
New Relic
Elasticsearch|ELK|Elastic Stack|OpenSearch
Kibana
Observability
Site Reliability Engineering|SRE
Infrastructure as Code|IaC
Nginx
Istio|service mesh
HashiCorp Vault
Apache Spark|Spark|PySpark|Spark SQL
Hadoop|HDFS
Hive
Kafka|Apache Kafka|Confluent
Flink|Apache Flink
Airflow|Apache Airflow
dbt
Snowflake
Databricks
Delta Lake
Apache Iceberg|Iceberg
ETL
ELT
Data pipelines|data pipeline
Data warehousing|data warehouse|EDW
Data lake|lakehouse|data lakehouse
Data modeling|data modelling|dimensional modeling|star schema
Data governance
Data quality
Master Data Management|MDM
Informatica
Talend
SSIS
SSRS
SSAS
Fivetran
Teradata
Oracle Database|Oracle DB|Oracle
PostgreSQL|Postgres
MySQL
SQL Server|MS SQL|MSSQL
MongoDB
Cassandra
Redis
Neo4j
Cosmos DB|CosmosDB
Presto|Trino
Big Data
Pandas
NumPy
Data analysis|data analytics
Data visualization|data visualisation
Tableau
Power BI|PowerBI
Looker
Qlik|QlikView|Qlik Sense
Excel|Microsoft Excel|MS Excel
Google Analytics
A/B testing|AB testing|experimentation
Statistics|statistical analysis|statistical modeling
SAS
SPSS
Predictive modeling|predictive analytics
Forecasting
KPIs|KPI
Dashboards|dashboard
Machine learning|ML
Deep learning
Artificial intelligence|AI
Generative AI|GenAI|Gen AI
LLMs|LLM|large language models|large language model
NLP|natural language processing
Computer vision
TensorFlow
PyTorch
Keras
scikit-learn|sklearn
XGBoost
Hugging Face|HuggingFace
LangChain
LlamaIndex
RAG|retrieval-augmented generation|retrieval augmented generation
Vector database|vector databases|vector DB|Pinecone|FAISS
Prompt engineering
Fine-tuning|fine tuning
MLOps
MLflow
Kubeflow
Feature engineering
Reinforcement learning
Recommendation systems|recommender systems
Time series
Anomaly detection
OpenCV
TensorRT
ONNX
Model deployment|model serving
Embeddings
Cybersecurity|cyber security
Information security|InfoSec
Network security
IAM|identity and access management
SSO|single sign-on
SAML
Okta
Active Directory|Entra ID|Azure AD
Zero Trust
SIEM
Security operations|SOC analyst|security operations center
Incident response
Vulnerability management
Penetration testing|pen testing
Threat modeling
Encryption|cryptography
PKI
Firewalls|firewall
NIST
ISO 27001
SOC 2|SOC2
PCI DSS|PCI
HIPAA
GDPR
FedRAMP
CISSP
CISM
Security+|CompTIA Security+
OWASP
DevSecOps
CrowdStrike
TCP/IP
DNS
BGP
OSPF
VPN
SD-WAN
LAN/WAN|LAN|WAN
Cisco
Juniper
Load balancing|load balancer
CCNA
CCNP
VMware|vSphere|ESXi
Virtualization
Citrix
Hyper-V
Disaster recovery|business continuity
ASIC
FPGA
RTL
DFT|design for test
Physical design
Timing closure|STA|static timing analysis
UVM
Design verification|functional verification|verification engineer
SoC|system on chip
PCIe
DDR
Ethernet
Embedded systems|embedded software|embedded C|embedded Linux
Firmware
RTOS
Device drivers|device driver
Linux kernel
Signal integrity
PCB
Cadence
Synopsys
EDA
Computer architecture
GPU|GPUs
HPC|high performance computing|high-performance computing
InfiniBand
MPI
OpenMP
Test automation|automated testing|automation testing
Selenium
Cypress
Playwright
JUnit
pytest
TestNG
Unit testing|unit tests
Integration testing
Regression testing
Performance testing|load testing
JMeter
LoadRunner
Quality assurance|QA
TDD|test-driven development
BDD|behavior-driven development
Cucumber
Postman
API testing
Agile
Scrum
Kanban
SAFe|Scaled Agile
Jira|JIRA
Confluence
Project management
Program management
Product management
Product roadmap|roadmap|roadmaps
Stakeholder management|stakeholders
PMP
Certified ScrumMaster|CSM|Scrum Master
PMO
Waterfall
SDLC|software development life cycle
Change management
Requirements gathering|requirements elicitation
Business requirements|BRD
User stories
Backlog|backlog grooming|backlog refinement
Risk management
Budgeting|budget management
Vendor management
Cross-functional|cross functional
OKRs
Go-to-market|GTM
Smartsheet
MS Project|Microsoft Project
Business analysis
Process improvement|continuous improvement
Process mapping|process modeling
Lean methodology|Lean manufacturing|Lean principles
Six Sigma|Lean Six Sigma
Root cause analysis|RCA
Gap analysis
UAT|user acceptance testing
SAP
SAP S/4HANA|S/4HANA|S4HANA
SAP FICO|SAP FI/CO
SAP MM
SAP SD
Oracle EBS|Oracle E-Business Suite
Oracle Cloud|OCI
NetSuite
Workday HCM|Workday
Salesforce|SFDC
ServiceNow
Dynamics 365|Microsoft Dynamics
CRM
ERP
HRIS
ITIL
ITSM
SharePoint
Power Automate
Power Apps|PowerApps
RPA|robotic process automation
UiPath
Alteryx
Financial analysis
Financial modeling|financial modelling
FP&A
Variance analysis
GAAP|US GAAP
IFRS
SOX|Sarbanes-Oxley
Internal audit|auditing|audit
Reconciliation|reconciliations
Accounts payable
Accounts receivable
General ledger
Month-end close|month end close
CPA
CFA
Valuation
M&A|mergers and acquisitions
Treasury
Hyperion
Anaplan
Credit risk
AML|anti-money laundering
KYC
Compliance|regulatory compliance
Regulatory reporting
EHR|EMR|electronic health records
Epic
Cerner
HL7
FHIR
ICD-10
Revenue cycle
Medicare
Medicaid
GxP
FDA
Clinical trials
GMP
CAPA
SEO
SEM
Digital marketing
Content marketing
Marketing automation
HubSpot
Marketo
Google Ads
Social media
Email marketing
Lead generation
Account management
Business development
B2B
B2C
Customer success
Enterprise sales
Solution selling
UX|user experience
UI|user interface
Figma
Sketch
Adobe XD
Photoshop
Illustrator
Wireframing|wireframes
Prototyping
User research
Usability testing
Design systems|design system
Accessibility|WCAG|a11y
Computer Science
MBA
PhD|Ph.D.
Master's degree|Master's|Masters degree|M.S.
"""

SOFT_SKILLS = r"""
Leadership|led teams|team lead
Mentoring|mentorship|coaching
Communication skills|communication
Collaboration|collaborative
Problem solving|problem-solving
Analytical skills|analytical
Critical thinking
Presentation skills|presentations
Customer service|customer-facing
Strategic planning
Decision making|decision-making
Time management
Attention to detail|detail-oriented
"""


def _parse(block, weight):
    out = []
    for line in block.strip().splitlines():
        parts = [p.strip() for p in line.split("|") if p.strip()]
        if parts:
            out.append((parts[0], parts, weight))
    return out


ENTRIES = _parse(HARD_SKILLS, 1.0) + _parse(SOFT_SKILLS, 0.5)
# Canonical names of the soft skills: tailoring writes them into their own sentence, never next to a tool.
SOFT = {canon for canon, _, weight in ENTRIES if weight < 1.0}

# Aliases that are also everyday English words; matched with exact case only.
CASE_SENSITIVE = {"Excel", "Spring", "Rust", "Swift", "Chef", "Puppet", "Epic", "Sketch", "Go", "Hive",
                  "Flask", "Helm", "Dart", "Julia", "Ruby", "Presto", "Looker", "Treasury", "Glue", "Lean"}

# Uppercase words that are never useful ATS keywords.
ACRONYM_STOP = {
    "US", "USA", "EEO", "EOE", "AA", "LLC", "INC", "CEO", "CFO", "CTO", "COO", "VP", "SVP", "EVP", "HR", "ID",
    "PTO", "ADA", "OFCCP", "DEI", "FAQ", "TBD", "ET", "PT", "EST", "PST", "CST", "MST", "OR", "AND", "THE",
    "NOTE", "FOR", "OF", "TO", "IN", "ON", "AT", "BY", "AN", "A", "I", "II", "III", "IV", "JR", "SR", "UK",
    "EU", "NA", "N/A", "OK", "YES", "NO", "WE", "OUR", "YOU", "ALL", "NEW", "PM", "AM", "BA", "BS", "MS",
    "MA", "BE", "IT", "IS", "AS", "SF", "NYC", "LA", "DC", "USD", "K", "M", "B", "E", "EEOC", "VEVRAA",
    "NOT", "REQ", "JD", "FT", "PTE", "WFH", "APAC", "EMEA", "AMER", "LATAM", "BASE", "PAY", "TOTAL",
}
