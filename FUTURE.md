A use case I just thought of for the near future goes something like
this:

Big, complex database exists of lots of information that there could
be benefit of having it exposed via a CRUD. So step one, we:

- Using something like db2openapi, or IBM OAS (preferably on
and freer end of the spectrum, to generate an openapi spec from the 
existing database.

- use fast-api-code-generator to automagically build a FastAPI webapp
based on the generated openai schema to create the pydantic models, the app routes, etc.

- Or, we take the more traditional approach and have SQLAlchemy create 
Python classes from the db, and generate Pydantic models, and let 
pydantic generate the openapi.spec from that. This is pretty involved 
though, so my hope was that for our purposes, since we only want the 
CRUD API to demonstrate our FUSE filesystem representation of it, that 
for our purposes it won't matter and we can take the fastest easiest 
aproach; whichever that turns out to be.

- Then we use the Pydantic models to create our FastAPI routes, Start 
the uvicorn server.

- then we can download the openapi.json direectly from the new api 
server. (Maybe this is a feature of apifuse by then too, so we just 
pass apifuse the url, and it does the rest, downloading the swagger 
spec before mounting its filesystem.

- demonstrate the simple, natural grace of accessing records from the 
db from your fingertips, subjecting the contents of the "files" to all 
your favorite unix "everything is a file" and "every i/o can be piped" 
tricks from muscle memory in the comfort of your shell.

