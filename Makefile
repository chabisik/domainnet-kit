default: send

send:
	git pull
	git add .
	git commit
	git push

