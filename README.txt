DIAGNOSTIKA VAG - bystryy start
================================

1. Raspakovat arhiv CELIKOM v otdelnuyu papku, naprimer C:\vagdiag
2. Vstavit adapter v razyom OBD pod rulyom sleva
3. VKLYUCHIT ZAZHIGANIE, dvigatel ne zapuskat
4. Podklyuchit kompyuter k seti Wi-Fi adaptera
5. Dvoynoy shchelchok po START_GUI.bat - otkroetsya brauzer

Python ustanavlivat ne nuzhno, on vnutri paketa.
Prava administratora ne trebuyutsya.

--------------------------------------------------------------------
FAYLY V PAPKE
--------------------------------------------------------------------
START_GUI.bat   glavnyy zapusk, interfeys v brauzere
START.bat       tekstovoe menyu
DEV_TOOLS.bat   instrumenty dlya pravki ishodnikov
README.md       polnoe opisanie na russkom
README.en.md    full description in English
ARCHITECTURE.md ustroystvo programmy dlya razrabotchikov
CONTRIBUTING.md kak dorabotat programmu
app\            ISHODNIKI - obychnye tekstovye fayly, mozhno pravit
tests\          avtotesty
python\         perenosimyy Python, ne trogat

--------------------------------------------------------------------
ISHODNIKI OTKRYTY
--------------------------------------------------------------------
Vsyo v papke app - eto obychnye tekstovye fayly. Otkryvayutsya lyubym
redaktorom, pravyatsya i rabotayut srazu. Nichego sobirat ne nuzhno.

Podrobnosti v faylah README.md i CONTRIBUTING.md - oni na russkom.

--------------------------------------------------------------------
PROVERKA BEZ AVTOMOBILYA
--------------------------------------------------------------------
Zapustit DEV_TOOLS.bat, punkt 3 - zaglushka adaptera.
Zatem v programme podklyuchitsya k adresu 127.0.0.1 port 35003.
