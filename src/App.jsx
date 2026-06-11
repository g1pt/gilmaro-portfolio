import {
  ArrowRight,
  CheckCircle2,
  Download,
  ExternalLink,
  Github,
  Mail,
  MapPin,
  Network,
} from "lucide-react";
import React, { useState } from "react";

const cvPath = "/cv/Gilmaro_Piter_CV_HouseOfBeta.pdf";
const originalCvPath = "/cv/Gilmaro_Piter_CV_Original.pdf";
const profilePath = "/profile/gilmaro-profile.jpg";
const email = "gilmaropiter@gmail.com";
const githubUrl = "https://github.com/g1pt/gilmaro-portfolio";

const projects = [
  {
    title: "OpenAxis",
    label: "Research & Monitoring Platform",
    description:
      "OpenAxis is een onderzoeks- en monitoringplatform voor het structureren, valideren en analyseren van datasets. Ik heb dit platform ontworpen en gebouwd met Python, FastAPI en PostgreSQL. De focus ligt op datakwaliteit, reproduceerbare analyses, monitoring en het inzichtelijk maken van resultaten via dashboards.",
    tech: ["Python", "FastAPI", "PostgreSQL", "Docker", "Data Pipelines"],
    impact: [
      "Minder handmatig analysewerk",
      "Centrale omgeving voor datasets en resultaten",
      "Reproduceerbare analyses",
      "Datakwaliteit en validatie",
      "Monitoring van processen en resultaten",
      "Schaalbare basis voor verdere uitbreiding",
    ],
  },
  {
    title: "Decision Support Logic",
    label: "Beslissingsondersteuning",
    description:
      "Beslisregels vertaald naar gestructureerde logica, logging en testbare processtappen.",
    tech: ["Python", "State Machines", "Event Logging", "Testing"],
    impact: ["Consistente analyse", "Betere uitlegbaarheid", "Minder losse interpretatie"],
  },
  {
    title: "Data Validation Engine",
    label: "Validatieomgeving",
    description:
      "Een omgeving voor het vergelijken en valideren van modellen over meerdere datasets.",
    tech: ["Python", "Pandas", "CSV Processing"],
    impact: ["Datagedreven vergelijking", "Kwaliteitscontrole", "Herhaalbare evaluatie"],
  },
];

const coreSkills = [
  "Python",
  "SQL",
  "PostgreSQL",
  "Git",
  "Data Analysis",
  "Process Automation",
];

const additionalSkills = [
  "FastAPI",
  "Docker",
  "Monitoring",
  "Dashboarding",
  "API Integrations",
  "Testing",
  "Webhooks",
  "CSV Processing",
  "Time-Series Analysis",
  "Research Methodology",
];

const interests = [
  "Data Analyse",
  "Applicatiebeheer",
  "Process Automation",
  "Data Engineering",
  "Monitoring & Reporting",
  "Business & IT",
];

const strengths = [
  "Analytisch en onderzoekend",
  "Sterke leerhouding",
  "Praktische projectervaring",
  "Discipline vanuit topsport",
  "Procesmatig denken",
  "Zelfstandig werken",
  "Samenwerken en communiceren",
  "Focus op continue verbetering",
];

function CvButton({ variant = "primary", href = cvPath, label = "Download CV" }) {
  return (
    <a className={`button button-${variant}`} href={href} target="_blank" rel="noreferrer" download>
      <Download size={18} aria-hidden="true" />
      {label}
    </a>
  );
}

function ProfilePhoto() {
  const [imageAvailable, setImageAvailable] = useState(true);

  return (
    <div className="profile-photo-card" aria-label="Profielfoto Gilmaro Piter">
      {imageAvailable ? (
        <img src={profilePath} alt="Gilmaro Piter" onError={() => setImageAvailable(false)} />
      ) : (
        <span>GP</span>
      )}
    </div>
  );
}

function SectionHeader({ eyebrow, title, text }) {
  return (
    <div className="section-header">
      <p className="eyebrow">{eyebrow}</p>
      <h2>{title}</h2>
      {text ? <p>{text}</p> : null}
    </div>
  );
}

function App() {
  return (
    <main>
      <header className="site-header">
        <a className="brand" href="#top" aria-label="Gilmaro Piter home">
          <span>GP</span>
          <strong>Gilmaro Piter</strong>
        </a>
        <nav aria-label="Hoofdnavigatie">
          <a href="#projects">Projecten</a>
          <a href="#skills">Skills</a>
          <a href="#house-of-beta">House of Bèta</a>
          <a href="#contact">Contact</a>
        </nav>
      </header>

      <section className="hero" id="top">
        <div className="hero-content">
          <p className="eyebrow">Junior Data & Systems Specialist</p>
          <h1>Data • Automation • Research Systems</h1>
          <p className="hero-subtitle">
            Ik bouw systemen die complexe processen inzichtelijk, meetbaar en automatiseerbaar maken.
          </p>
          <p className="hero-text">
            Met een achtergrond in topsport en jarenlange zelfstudie werk ik aan projecten rond
            data-analyse, automatisering, monitoring en onderzoeksplatformen.
          </p>
          <div className="badge-row" aria-label="Kerninformatie">
            <span>Beschikbaar 32+ uur</span>
            <span>Regio Rotterdam</span>
            <span>Open voor traineeships</span>
            <span>Data & Automation</span>
          </div>
          <div className="hero-actions">
            <a className="button button-primary" href="#projects">
              Bekijk projecten
              <ArrowRight size={18} aria-hidden="true" />
            </a>
            <CvButton variant="secondary" />
            <a className="button button-secondary" href={githubUrl} target="_blank" rel="noreferrer">
              <Github size={18} aria-hidden="true" />
              Bekijk GitHub
              <ExternalLink size={15} aria-hidden="true" />
            </a>
            <a className="button button-ghost" href={`mailto:${email}`}>
              <Mail size={18} aria-hidden="true" />
              Neem contact op
            </a>
          </div>
        </div>

        <aside className="hero-panel" aria-label="Profiel samenvatting">
          <ProfilePhoto />
          <div className="signal-card">
            <span className="signal-dot" />
            <p>Richting</p>
            <strong>Junior Data & Systems Specialist</strong>
          </div>
          <div className="metric-grid">
            <div>
              <span>Focus</span>
              <strong>Data, processen, dashboards</strong>
            </div>
            <div>
              <span>Basis</span>
              <strong>Python, SQL, Git, PostgreSQL</strong>
            </div>
            <div>
              <span>Werkstijl</span>
              <strong>Leerbaar, rustig, praktisch</strong>
            </div>
            <div>
              <span>Beschikbaar</span>
              <strong>Vanaf 32 uur per week</strong>
            </div>
          </div>
        </aside>
      </section>

      <section className="section about" id="about">
        <SectionHeader eyebrow="Over mij" title="Niet traditioneel, wel doelgericht" />
        <div className="about-grid">
          <div className="copy-stack">
            <p>
              Mijn achtergrond ligt niet in een traditionele IT-opleiding. Tien jaar topsport heeft
              gezorgd voor discipline, doorzettingsvermogen, samenwerken onder druk en continue
              verbetering.
            </p>
            <p>
              Diezelfde mentaliteit pas ik toe op data, software en automatisering. Ik heb
              zelfstandig projecten gebouwd waarin data, processen en technische oplossingen
              samenkomen.
            </p>
            <p>
              Mijn interesse begon binnen financiële markten, maar de kern ligt breder: data
              structureren, processen modelleren, systemen bouwen en resultaten meetbaar maken.
            </p>
          </div>
          <div className="profile-card">
            <Network size={28} aria-hidden="true" />
            <h3>Onderzoeks- en datasystemen</h3>
            <p>
              Ondersteunend profiel: research systems, datastromen, validatie, logging en
              dashboards.
            </p>
          </div>
        </div>
      </section>

      <section className="section seek" id="wat-ik-zoek">
        <SectionHeader eyebrow="Wat ik zoek" title="Junior rol of traineeship" />
        <div className="seek-card">
          <p>
            Ik zoek een junior functie of traineeship waarin ik verder kan groeien binnen data,
            automatisering, applicatiebeheer en procesverbetering.
          </p>
          <p>
            Mijn voorkeur gaat uit naar een omgeving waar leren in de praktijk, begeleiding en
            samenwerking centraal staan.
          </p>
          <div className="interest-list">
            {interests.map((interest) => (
              <span key={interest}>{interest}</span>
            ))}
          </div>
        </div>
      </section>

      <section className="section strengths" id="strengths">
        <SectionHeader eyebrow="Sterke punten" title="Wat ik meeneem" />
        <div className="strength-grid">
          {strengths.map((strength) => (
            <span key={strength}>
              <CheckCircle2 size={17} aria-hidden="true" />
              {strength}
            </span>
          ))}
        </div>
      </section>

      <section className="section" id="skills">
        <SectionHeader
          eyebrow="Skills"
          title="Technische basis"
          text="Kernvaardigheden eerst, aangevuld met tools en methodes uit eigen projecten."
        />
        <div className="skills-layout">
          <div>
            <h3>Kernvaardigheden</h3>
            <div className="skill-cloud">
              {coreSkills.map((skill) => (
                <span key={skill}>{skill}</span>
              ))}
            </div>
          </div>
          <div>
            <h3>Aanvullend</h3>
            <div className="skill-cloud">
              {additionalSkills.map((skill) => (
                <span key={skill}>{skill}</span>
              ))}
            </div>
          </div>
        </div>
      </section>

      <section className="section projects" id="projects">
        <SectionHeader
          eyebrow="Projecten"
          title="Praktische projectervaring"
          text="Data, validatie, workflow, logging, dashboards en systeemontwerp."
        />
        <div className="project-list">
          {projects.map((project) => (
            <article className="project-card" key={project.title}>
              <div className="project-main">
                <span className="project-label">{project.label}</span>
                <h3>{project.title}</h3>
                <p>{project.description}</p>
                <div className="tech-row">
                  {project.tech.map((tech) => (
                    <span key={tech}>{tech}</span>
                  ))}
                </div>
              </div>
              <div className="impact-box">
                <strong>Waarde</strong>
                <ul>
                  {project.impact.map((item) => (
                    <li key={item}>
                      <CheckCircle2 size={16} aria-hidden="true" />
                      {item}
                    </li>
                  ))}
                </ul>
              </div>
            </article>
          ))}
        </div>
      </section>

      <section className="section house-beta" id="house-of-beta">
        <div>
          <p className="eyebrow">Waarom House of Bèta</p>
          <h2>Analytisch leren in echte projecten</h2>
          <p>
            Ik ben op zoek naar een omgeving waarin analytisch denken, leren in de praktijk en het
            oplossen van complexe vraagstukken centraal staan.
          </p>
          <p>
            House of Bèta spreekt mij aan omdat het ruimte biedt om te groeien, praktijkervaring op
            te doen en waarde toe te voegen aan echte projecten.
          </p>
        </div>
        <div className="house-beta-actions">
          <CvButton variant="primary" />
          <a className="button button-ghost" href={originalCvPath} target="_blank" rel="noreferrer">
            Originele CV bekijken
          </a>
        </div>
      </section>

      <section className="section recruiter">
        <div>
          <p className="eyebrow">Recruiter Samenvatting</p>
          <h2>Praktisch, analytisch en leerbaar</h2>
          <p>
            Junior Data & Systems Specialist met een achtergrond in topsport, onderwijs en
            zelfstudie richting IT. Ervaring met Python, SQL, FastAPI, PostgreSQL, Docker,
            monitoring, dashboards, API-integraties en procesautomatisering.
          </p>
        </div>
        <div className="focus-grid">
          {strengths.slice(0, 6).map((point) => (
            <span key={point}>
              <CheckCircle2 size={17} aria-hidden="true" />
              {point}
            </span>
          ))}
        </div>
      </section>

      <section className="section contact" id="contact">
        <SectionHeader
          eyebrow="Contact"
          title="Open voor junior rollen en traineeships"
          text="Data, automatisering, applicatiebeheer en procesverbetering."
        />
        <div className="contact-card">
          <div className="contact-details">
            <p>
              <strong>Naam</strong>
              <span>Gilmaro Piter</span>
            </p>
            <p>
              <strong>Email</strong>
              <a href={`mailto:${email}`}>{email}</a>
            </p>
            <p>
              <strong>Regio</strong>
              <span>Rotterdam / Nederland</span>
            </p>
            <p>
              <strong>GitHub</strong>
              <a href={githubUrl} target="_blank" rel="noreferrer">
                g1pt/gilmaro-portfolio
              </a>
            </p>
          </div>
          <div className="contact-actions">
            <a className="button button-primary" href={`mailto:${email}`}>
              <Mail size={18} aria-hidden="true" />
              Mail mij
            </a>
            <a className="button button-secondary" href={githubUrl} target="_blank" rel="noreferrer">
              <Github size={18} aria-hidden="true" />
              Bekijk GitHub
              <ExternalLink size={15} aria-hidden="true" />
            </a>
            <CvButton variant="ghost" />
            <a className="button button-ghost" href={originalCvPath} target="_blank" rel="noreferrer">
              Originele CV bekijken
            </a>
          </div>
        </div>
      </section>

      <footer>
        <span>Gilmaro Piter</span>
        <span>
          <MapPin size={15} aria-hidden="true" />
          Rotterdam / Nederland
        </span>
      </footer>
    </main>
  );
}

export default App;
